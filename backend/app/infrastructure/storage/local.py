from __future__ import annotations

import html
import re
import zipfile
from io import BytesIO
from pathlib import Path

from app.core.errors import AppError

MAX_EPUB_UNCOMPRESSED_BYTES = 100 * 1024 * 1024  # 100 MiB zip-bomb guard


class LocalStorage:
    """
    Local volume storage isolated by novel_id (ADR-08).
    Enforces strict path traversal protection.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        (self.root / "novels").mkdir(parents=True, exist_ok=True)
        (self.root / "exports").mkdir(parents=True, exist_ok=True)

    def _safe_resolve(self, rel_path: str) -> Path:
        candidate = (self.root / rel_path).resolve()
        if not str(candidate).startswith(str(self.root)):
            raise AppError(
                code="INVALID_STORAGE_PATH",
                message="Attempted path traversal outside storage root",
                status_code=400,
            )
        return candidate

    def save_novel_source(self, novel_id: str, filename: str, data: bytes) -> str:
        safe_name = re.sub(r"[^a-zA-Z0-9._\-\u4e00-\u9fff]", "_", Path(filename).name)
        rel = f"novels/{novel_id}/source/{safe_name}"
        target = self._safe_resolve(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return rel

    def save_chapter_text(self, novel_id: str, source_version: int, chapter_no: int, text: str) -> str:
        rel = f"novels/{novel_id}/v{source_version}/chapters/{chapter_no:05d}.txt"
        target = self._safe_resolve(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return rel

    def read_text(self, rel_path: str) -> str:
        target = self._safe_resolve(rel_path)
        if not target.exists():
            raise AppError(
                code="FILE_NOT_FOUND",
                message="Stored chapter content not found",
                status_code=404,
            )
        return target.read_text(encoding="utf-8")

    def save_export(self, novel_id: str, export_id: str, ext: str, data: bytes) -> tuple[str, int]:
        rel = f"exports/{novel_id}/{export_id}.{ext}"
        target = self._safe_resolve(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return rel, len(data)

    def get_export_path(self, rel_path: str) -> Path:
        target = self._safe_resolve(rel_path)
        if not target.exists():
            raise AppError(
                code="EXPORT_NOT_FOUND",
                message="Export file not found on storage",
                status_code=404,
            )
        return target


def extract_text_from_epub(raw_bytes: bytes) -> str:
    """
    Safely extract plain text from an EPUB archive with zip-slip & zip-bomb guards (Section 11).
    """
    try:
        zf = zipfile.ZipFile(BytesIO(raw_bytes))
    except zipfile.BadZipFile as exc:
        raise AppError(code="INVALID_EPUB", message="Corrupted EPUB archive", status_code=400) from exc

    total_uncompressed = 0
    html_parts: list[str] = []
    with zf:
        for info in sorted(zf.infolist(), key=lambda i: i.filename):
            if ".." in Path(info.filename).parts or info.filename.startswith("/"):
                raise AppError(
                    code="UNSAFE_ARCHIVE",
                    message="Unsafe file path detected in EPUB archive",
                    status_code=400,
                )
            total_uncompressed += info.file_size
            if total_uncompressed > MAX_EPUB_UNCOMPRESSED_BYTES:
                raise AppError(
                    code="ARCHIVE_TOO_LARGE",
                    message="EPUB uncompressed size exceeds safety limit",
                    status_code=400,
                )
            lower = info.filename.lower()
            if lower.endswith((".xhtml", ".html", ".htm")):
                raw_html = zf.read(info).decode("utf-8", errors="replace")
                text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", raw_html, flags=re.S | re.I)
                text = re.sub(r"</(p|div|h[1-6]|br|li)>", "\n", text, flags=re.I)
                text = re.sub(r"<[^>]+>", "", text)
                html_parts.append(html.unescape(text).strip())

    return "\n\n".join(p for p in html_parts if p)
