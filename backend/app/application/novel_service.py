from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError
from app.domain.text_processing import (
    chunk_chapter_text,
    detect_and_decode,
    parse_chapters,
    sha256_bytes,
)
from app.infrastructure.db.models import Chapter, Chunk, Novel
from app.infrastructure.storage.local import LocalStorage, extract_text_from_epub


def ingest_novel_file(
    db: Session,
    storage: LocalStorage,
    settings: Settings,
    *,
    title: str,
    author: str | None,
    filename: str,
    raw_bytes: bytes,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
) -> Novel:
    if not raw_bytes:
        raise AppError(code="EMPTY_FILE", message="Uploaded file is empty", status_code=400)

    if len(raw_bytes) > settings.max_upload_bytes:
        raise AppError(
            code="FILE_TOO_LARGE",
            message=f"File exceeds maximum allowed size ({settings.max_upload_bytes} bytes)",
            status_code=413,
        )

    ext = Path(filename).suffix.lower().lstrip(".")
    if ext == "pdf":
        raise AppError(
            code="UNSUPPORTED_PDF",
            message="PDF text extraction is unreliable for full-novel analysis; please convert to TXT or EPUB first",
            status_code=400,
        )
    if ext not in ("txt", "epub"):
        raise AppError(
            code="UNSUPPORTED_FORMAT",
            message="Only TXT and EPUB files are supported",
            status_code=400,
        )

    if ext == "epub":
        raw_text = extract_text_from_epub(raw_bytes)
        encoding = "utf-8"
    else:
        raw_text, encoding = detect_and_decode(raw_bytes)

    parsed_chapters, diagnostics = parse_chapters(raw_text, encoding=encoding)
    if not parsed_chapters:
        raise AppError(
            code="NO_TEXT_CONTENT",
            message="No readable text content found in uploaded file",
            status_code=400,
        )

    source_hash = sha256_bytes(raw_bytes)
    novel = Novel(
        title=title.strip() or Path(filename).stem,
        author=author.strip() if author else None,
        source_filename=Path(filename).name,
        source_format=ext,
        source_hash=source_hash,
        source_version=1,
        source_storage_ref="",
        encoding=encoding,
        total_chars=diagnostics.total_chars,
        chapter_count=len(parsed_chapters),
        chunk_count=0,
        status="ready",
        diagnostics_json=asdict(diagnostics),
    )
    db.add(novel)
    db.flush()

    novel.source_storage_ref = storage.save_novel_source(novel.id, filename, raw_bytes)

    eff_chunk_size = chunk_size or settings.default_chunk_size
    eff_overlap = chunk_overlap if chunk_overlap is not None else settings.default_chunk_overlap
    total_chunks = 0

    for pch in parsed_chapters:
        content_ref = storage.save_chapter_text(
            novel.id, novel.source_version, pch.chapter_no, pch.content
        )
        chapter = Chapter(
            novel_id=novel.id,
            source_version=novel.source_version,
            chapter_no=pch.chapter_no,
            declared_no=pch.declared_no,
            title=pch.title[:255],
            char_count=pch.char_count,
            text_hash=pch.text_hash,
            content_ref=content_ref,
        )
        db.add(chapter)
        db.flush()

        text_chunks = chunk_chapter_text(
            pch.content,
            chunk_size=eff_chunk_size,
            overlap=eff_overlap,
        )
        for tc in text_chunks:
            db.add(
                Chunk(
                    novel_id=novel.id,
                    chapter_id=chapter.id,
                    chunk_index=tc.chunk_index,
                    start_offset=tc.start_offset,
                    end_offset=tc.end_offset,
                    text_hash=tc.text_hash,
                    processing_status="pending",
                )
            )
            total_chunks += 1

    novel.chunk_count = total_chunks
    db.commit()
    db.refresh(novel)
    return novel


def list_novels(db: Session, page: int = 1, page_size: int = 20) -> tuple[list[Novel], int]:
    total = db.scalar(select(func.count()).select_from(Novel)) or 0
    rows = (
        db.scalars(
            select(Novel)
            .order_by(Novel.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        .all()
    )
    return list(rows), total


def get_novel(db: Session, novel_id: str) -> Novel:
    novel = db.get(Novel, novel_id)
    if not novel:
        raise AppError(code="NOVEL_NOT_FOUND", message="Novel not found", status_code=404)
    return novel


def list_chapters(
    db: Session,
    novel_id: str,
    *,
    page: int = 1,
    page_size: int = 50,
    search: str | None = None,
) -> tuple[list[Chapter], int]:
    get_novel(db, novel_id)
    stmt = select(Chapter).where(Chapter.novel_id == novel_id)
    count_stmt = select(func.count()).select_from(Chapter).where(Chapter.novel_id == novel_id)
    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.where(Chapter.title.ilike(like))
        count_stmt = count_stmt.where(Chapter.title.ilike(like))

    total = db.scalar(count_stmt) or 0
    rows = (
        db.scalars(
            stmt.order_by(Chapter.chapter_no.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        .all()
    )
    return list(rows), total


def get_chapter_with_content(
    db: Session,
    storage: LocalStorage,
    chapter_id: str,
) -> tuple[Chapter, str, list[Chunk]]:
    chapter = db.get(Chapter, chapter_id)
    if not chapter:
        raise AppError(code="CHAPTER_NOT_FOUND", message="Chapter not found", status_code=404)
    content = storage.read_text(chapter.content_ref)
    chunks = list(
        db.scalars(
            select(Chunk).where(Chunk.chapter_id == chapter.id).order_by(Chunk.chunk_index.asc())
        ).all()
    )
    return chapter, content, chunks
