from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.domain.text_processing import codepoint_span_to_utf16
from app.infrastructure.db.models import Chapter, Evidence
from app.infrastructure.storage.local import LocalStorage


def get_evidence_detail(
    db: Session,
    storage: LocalStorage,
    evidence_id: str,
    context_chars: int = 160,
) -> dict:
    ev = db.get(Evidence, evidence_id)
    if not ev:
        raise AppError(code="EVIDENCE_NOT_FOUND", message="Evidence not found", status_code=404)

    chapter = db.get(Chapter, ev.chapter_id)
    if not chapter:
        raise AppError(code="CHAPTER_NOT_FOUND", message="Associated chapter not found", status_code=404)

    chapter_text = storage.read_text(chapter.content_ref)
    ctx_start = max(0, ev.start_offset - context_chars)
    ctx_end = min(len(chapter_text), ev.end_offset + context_chars)
    context_text = chapter_text[ctx_start:ctx_end]

    # Exact UTF-16 offsets inside chapter text & inside context snippet for JS frontend
    ch_utf16_start, ch_utf16_end = codepoint_span_to_utf16(
        chapter_text, ev.start_offset, ev.end_offset
    )
    rel_start = max(0, ev.start_offset - ctx_start)
    rel_end = max(rel_start, ev.end_offset - ctx_start)
    ctx_utf16_start, ctx_utf16_end = codepoint_span_to_utf16(context_text, rel_start, rel_end)

    return {
        "id": ev.id,
        "novel_id": ev.novel_id,
        "chapter_id": ev.chapter_id,
        "chapter_no": chapter.chapter_no,
        "chapter_title": chapter.title,
        "start_offset": ev.start_offset,
        "end_offset": ev.end_offset,
        "utf16_start_offset": ch_utf16_start,
        "utf16_end_offset": ch_utf16_end,
        "quote_text": ev.quote_text,
        "text_hash": ev.text_hash,
        "verification_status": ev.verification_status,
        "verification_note": ev.verification_note,
        "context_text": context_text,
        "context_utf16_start": ctx_utf16_start,
        "context_utf16_end": ctx_utf16_end,
    }
