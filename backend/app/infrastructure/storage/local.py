from __future__ import annotations

import html
import posixpath
import re
import xml.etree.ElementTree as ET
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
        (self.root / "audio").mkdir(parents=True, exist_ok=True)

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

    def save_audio_bytes(self, rel_path: str, data: bytes) -> tuple[str, int]:
        rel = f"audio/{rel_path.lstrip('/')}"
        target = self._safe_resolve(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return rel, len(data)

    def prepare_audio_path(self, rel_path: str) -> tuple[str, Path]:
        """Return (rel_ref, absolute_path) with parent directories created for streaming disk writes."""
        rel = f"audio/{rel_path.lstrip('/')}"
        target = self._safe_resolve(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        return rel, target

    def get_audio_path(self, rel_path: str) -> Path:
        target = self._safe_resolve(rel_path)
        if not target.exists():
            raise AppError(
                code="AUDIO_NOT_FOUND",
                message="Audio file not found on storage",
                status_code=404,
            )
        return target

    def read_bytes(self, rel_path: str) -> bytes:
        target = self._safe_resolve(rel_path)
        if not target.exists():
            raise AppError(
                code="FILE_NOT_FOUND",
                message="Stored binary file not found",
                status_code=404,
            )
        return target.read_bytes()


def _strip_html(raw_html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", raw_html, flags=re.S | re.I)
    text = re.sub(r"</(p|div|h[1-6]|br|li)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _resolve_epub_spine_paths(zf: zipfile.ZipFile) -> list[str] | None:
    """
    FIX-05: Parse META-INF/container.xml -> OPF manifest + spine reading order.
    Returns ordered archive member names, or None if OPF spine is missing/unparseable.
    """
    names = set(zf.namelist())
    opf_path: str | None = None

    if "META-INF/container.xml" in names:
        try:
            root = ET.fromstring(zf.read("META-INF/container.xml"))
            for elem in root.iter():
                if elem.tag.endswith("rootfile"):
                    full_path = elem.attrib.get("full-path")
                    if full_path and full_path in names:
                        opf_path = full_path
                        break
        except ET.ParseError:
            opf_path = None

    if opf_path is None:
        for name in sorted(names):
            if name.lower().endswith(".opf"):
                opf_path = name
                break

    if not opf_path:
        return None

    try:
        opf_root = ET.fromstring(zf.read(opf_path))
    except (ET.ParseError, KeyError):
        return None

    opf_dir = posixpath.dirname(opf_path)
    manifest: dict[str, str] = {}
    spine_ids: list[str] = []

    for elem in opf_root.iter():
        tag = elem.tag.rsplit("}", 1)[-1]
        if tag == "item":
            item_id = elem.attrib.get("id")
            href = elem.attrib.get("href")
            if item_id and href:
                joined = posixpath.normpath(posixpath.join(opf_dir, href)) if opf_dir else posixpath.normpath(href)
                manifest[item_id] = joined
        elif tag == "itemref":
            idref = elem.attrib.get("idref")
            if idref:
                spine_ids.append(idref)

    if not spine_ids:
        return None

    ordered: list[str] = []
    for idref in spine_ids:
        target = manifest.get(idref)
        if target and target in names:
            ordered.append(target)

    return ordered if ordered else None


def extract_text_from_epub_with_meta(raw_bytes: bytes) -> tuple[str, dict[str, object]]:
    """
    FIX-05: Safely extract plain text from an EPUB archive with zip-slip & zip-bomb guards,
    preferring OPF spine reading order and returning diagnostics if fallback sorting was used.
    """
    try:
        zf = zipfile.ZipFile(BytesIO(raw_bytes))
    except zipfile.BadZipFile as exc:
        raise AppError(code="INVALID_EPUB", message="Corrupted EPUB archive", status_code=400) from exc

    total_uncompressed = 0
    html_parts: list[str] = []
    meta: dict[str, object] = {"epub_spine_used": False, "epub_order_warning": None}

    with zf:
        for info in zf.infolist():
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

        spine_paths = _resolve_epub_spine_paths(zf)
        if spine_paths:
            meta["epub_spine_used"] = True
            ordered_files = [
                p for p in spine_paths if p.lower().endswith((".xhtml", ".html", ".htm"))
            ]
        else:
            meta["epub_spine_used"] = False
            meta["epub_order_warning"] = (
                "OPF spine not found or invalid; fell back to archive filename order. "
                "Please verify chapter ordering."
            )
            ordered_files = [
                info.filename
                for info in sorted(zf.infolist(), key=lambda i: i.filename)
                if info.filename.lower().endswith((".xhtml", ".html", ".htm"))
            ]

        for member in ordered_files:
            raw_html = zf.read(member).decode("utf-8", errors="replace")
            stripped = _strip_html(raw_html)
            if stripped:
                html_parts.append(stripped)

    return "\n\n".join(html_parts), meta


def extract_text_from_epub(raw_bytes: bytes) -> str:
    """Backward-compatible wrapper returning plain text extracted from EPUB."""
    text, _meta = extract_text_from_epub_with_meta(raw_bytes)
    return text
