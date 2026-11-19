from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import get_storage
from app.api.schemas import (
    ChapterDetailOut,
    ChapterSummaryOut,
    ChunkOut,
    NovelOut,
    PaginatedResponse,
)
from app.application import novel_service
from app.core.config import Settings, get_settings
from app.core.security import Principal, UserRole, require_roles
from app.infrastructure.db.session import get_db
from app.infrastructure.storage.local import LocalStorage

router = APIRouter(tags=["novels"])


@router.post("/novels", response_model=NovelOut, status_code=status.HTTP_201_CREATED)
async def create_novel(
    file: UploadFile = File(...),
    title: str = Form(default=""),
    author: str | None = Form(default=None),
    chunk_size: int | None = Form(default=None),
    chunk_overlap: int | None = Form(default=None),
    db: Session = Depends(get_db),
    storage: LocalStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN)),
):
    raw_bytes = await file.read()
    novel = novel_service.ingest_novel_file(
        db,
        storage,
        settings,
        title=title,
        author=author,
        filename=file.filename or "novel.txt",
        raw_bytes=raw_bytes,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )
    return novel


@router.get("/novels", response_model=PaginatedResponse[NovelOut])
def list_novels(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    items, total = novel_service.list_novels(db, page=page, page_size=page_size)
    return PaginatedResponse(items=items, page=page, page_size=page_size, total=total)


@router.get("/novels/{novel_id}", response_model=NovelOut)
def get_novel(
    novel_id: str,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    return novel_service.get_novel(db, novel_id)


@router.get("/novels/{novel_id}/chapters", response_model=PaginatedResponse[ChapterSummaryOut])
def list_chapters(
    novel_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    search: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    items, total = novel_service.list_chapters(
        db, novel_id, page=page, page_size=page_size, search=search
    )
    return PaginatedResponse(items=items, page=page, page_size=page_size, total=total)


@router.get("/chapters/{chapter_id}", response_model=ChapterDetailOut)
def get_chapter(
    chapter_id: str,
    db: Session = Depends(get_db),
    storage: LocalStorage = Depends(get_storage),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER)),
):
    chapter, content, chunks = novel_service.get_chapter_with_content(db, storage, chapter_id)
    return ChapterDetailOut(
        id=chapter.id,
        novel_id=chapter.novel_id,
        chapter_no=chapter.chapter_no,
        declared_no=chapter.declared_no,
        title=chapter.title,
        char_count=chapter.char_count,
        text_hash=chapter.text_hash,
        content=content,
        chunks=[ChunkOut.model_validate(c) for c in chunks],
    )
