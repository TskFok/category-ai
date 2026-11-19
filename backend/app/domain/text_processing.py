from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

from charset_normalizer import from_bytes


# Matches common Chinese web novel chapter headings, e.g.:
# 第一章 忘忧酒馆, 第123回 ..., 第两千三百四十五节 ..., 楔子, 序章, 尾声
CHAPTER_HEADING_RE = re.compile(
    r"^(?:第[零一二三四五六七八九十百千万两0-9]+[章回节卷集部篇话](?:[ \t　:：\-—·\.].*)?|楔子(?:[ \t　].*)?|序章(?:[ \t　].*)?|引子(?:[ \t　].*)?|尾声(?:[ \t　].*)?)$"
)

CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
CHINESE_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}


@dataclass(frozen=True)
class ParsedChapter:
    chapter_no: int
    title: str
    content: str
    text_hash: str
    char_count: int
    declared_no: int | None = None


@dataclass(frozen=True)
class TextChunk:
    chunk_index: int
    start_offset: int
    end_offset: int
    text: str
    text_hash: str


@dataclass(frozen=True)
class ParseDiagnostics:
    encoding: str
    total_chars: int
    chapter_count: int
    missing_chapter_numbers: list[int]
    duplicate_chapter_titles: list[str]
    garbled_ratio: float


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def detect_and_decode(raw_bytes: bytes) -> tuple[str, str]:
    """Detect encoding and decode raw bytes cleanly without corrupting Chinese text."""
    if not raw_bytes:
        return "", "utf-8"

    # Try strict UTF-8 / UTF-8-SIG first
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return raw_bytes.decode(enc), enc
        except UnicodeDecodeError:
            pass

    # Try GB18030 (superset of GBK / GB2312)
    try:
        text = raw_bytes.decode("gb18030")
        return text, "gb18030"
    except UnicodeDecodeError:
        pass

    # Fallback to charset-normalizer
    best = from_bytes(raw_bytes).best()
    if best is not None:
        return str(best), best.encoding or "unknown"

    return raw_bytes.decode("utf-8", errors="replace"), "utf-8-replace"


def normalize_text(text: str) -> str:
    """
    Produce a deterministic normalized chapter/novel text.
    - Normalizes line endings to \\n
    - Applies NFC Unicode normalization
    - Strips trailing spaces on lines
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFC", text)
    lines = [line.rstrip(" \t\u3000") for line in text.split("\n")]
    return "\n".join(lines).strip()


def parse_chinese_number(num_str: str) -> int | None:
    if num_str.isdigit():
        return int(num_str)
    total = 0
    section = 0
    current = 0
    for ch in num_str:
        if ch in CHINESE_DIGITS:
            current = CHINESE_DIGITS[ch]
        elif ch in CHINESE_UNITS:
            unit = CHINESE_UNITS[ch]
            if unit == 10000:
                section = (section + current) * unit
                total += section
                section = 0
                current = 0
            else:
                if current == 0 and unit == 10 and section == 0:
                    current = 1
                section += current * unit
                current = 0
        else:
            return None
    return total + section + current


def _extract_declared_chapter_no(title: str) -> int | None:
    m = re.match(r"^第([零一二三四五六七八九十百千万两0-9]+)[章回节卷集部篇话]", title)
    if not m:
        return None
    return parse_chinese_number(m.group(1))


def estimate_garbled_ratio(text: str) -> float:
    if not text:
        return 0.0
    replacement_count = text.count("\ufffd")
    return round(replacement_count / len(text), 6)


def parse_chapters(raw_text: str, encoding: str = "utf-8") -> tuple[list[ParsedChapter], ParseDiagnostics]:
    """
    Split normalized novel text into sequential chapters.
    Chapter ordering is strictly sequential (1..N) rather than trusting headings alone.
    """
    normalized = normalize_text(raw_text)
    if not normalized:
        return [], ParseDiagnostics(
            encoding=encoding,
            total_chars=0,
            chapter_count=0,
            missing_chapter_numbers=[],
            duplicate_chapter_titles=[],
            garbled_ratio=0.0,
        )

    lines = normalized.split("\n")
    heading_indices: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped and len(stripped) <= 80 and CHAPTER_HEADING_RE.match(stripped):
            heading_indices.append((idx, stripped))

    chapters: list[ParsedChapter] = []
    if not heading_indices:
        # Fallback: treat entire text as single chapter
        chapters.append(
            ParsedChapter(
                chapter_no=1,
                title="正文",
                content=normalized,
                text_hash=sha256_text(normalized),
                char_count=len(normalized),
                declared_no=1,
            )
        )
    else:
        # Handle any prologue text before first heading
        first_idx = heading_indices[0][0]
        if first_idx > 0:
            preface = "\n".join(lines[:first_idx]).strip()
            if len(preface) >= 20:
                chapters.append(
                    ParsedChapter(
                        chapter_no=len(chapters) + 1,
                        title="序言",
                        content=preface,
                        text_hash=sha256_text(preface),
                        char_count=len(preface),
                        declared_no=0,
                    )
                )

        for i, (start_line, title) in enumerate(heading_indices):
            end_line = heading_indices[i + 1][0] if i + 1 < len(heading_indices) else len(lines)
            body = "\n".join(lines[start_line:end_line]).strip()
            if not body:
                continue
            chapters.append(
                ParsedChapter(
                    chapter_no=len(chapters) + 1,
                    title=title,
                    content=body,
                    text_hash=sha256_text(body),
                    char_count=len(body),
                    declared_no=_extract_declared_chapter_no(title),
                )
            )

    # Diagnostics: duplicate titles & missing declared numbers
    seen_titles: set[str] = set()
    duplicate_titles: list[str] = []
    declared_nums: list[int] = []
    for ch in chapters:
        if ch.title in seen_titles and ch.title not in duplicate_titles:
            duplicate_titles.append(ch.title)
        seen_titles.add(ch.title)
        if ch.declared_no is not None and ch.declared_no > 0:
            declared_nums.append(ch.declared_no)

    missing_nums: list[int] = []
    if declared_nums:
        num_set = set(declared_nums)
        min_no, max_no = min(declared_nums), max(declared_nums)
        if max_no - min_no <= 20000:
            missing_nums = [n for n in range(min_no, max_no + 1) if n not in num_set][:100]

    total_chars = sum(c.char_count for c in chapters)
    diagnostics = ParseDiagnostics(
        encoding=encoding,
        total_chars=total_chars,
        chapter_count=len(chapters),
        missing_chapter_numbers=missing_nums,
        duplicate_chapter_titles=duplicate_titles,
        garbled_ratio=estimate_garbled_ratio(normalized),
    )
    return chapters, diagnostics


def chunk_chapter_text(
    chapter_text: str,
    chunk_size: int = 4500,
    overlap: int = 400,
) -> list[TextChunk]:
    """
    Split a single chapter into overlapping chunks aligned to paragraph/sentence boundaries
    when possible, preserving exact Unicode code-point offsets [start_offset, end_offset).
    Never merges across chapters.
    """
    if not chapter_text:
        return []
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be >= 0 and < chunk_size")

    text_len = len(chapter_text)
    if text_len <= chunk_size:
        return [
            TextChunk(
                chunk_index=0,
                start_offset=0,
                end_offset=text_len,
                text=chapter_text,
                text_hash=sha256_text(chapter_text),
            )
        ]

    chunks: list[TextChunk] = []
    start = 0
    idx = 0
    while start < text_len:
        raw_end = min(start + chunk_size, text_len)
        end = raw_end

        # Prefer splitting on a paragraph break or sentence punctuation near raw_end
        if raw_end < text_len:
            search_floor = start + int(chunk_size * 0.7)
            best_break = chapter_text.rfind("\n", search_floor, raw_end)
            if best_break == -1:
                for punct in ("。", "！", "？", "”"):
                    pos = chapter_text.rfind(punct, search_floor, raw_end)
                    if pos != -1:
                        best_break = pos + 1
                        break
            if best_break != -1 and best_break > start:
                end = best_break

        chunk_str = chapter_text[start:end]
        chunks.append(
            TextChunk(
                chunk_index=idx,
                start_offset=start,
                end_offset=end,
                text=chunk_str,
                text_hash=sha256_text(chunk_str),
            )
        )
        idx += 1
        if end >= text_len:
            break
        next_start = max(end - overlap, start + 1)
        start = next_start

    return chunks


def codepoint_to_utf16_offset(text: str, codepoint_offset: int) -> int:
    """
    Convert a Python Unicode code-point index into a JS UTF-16 code-unit index.
    Characters outside the BMP (ord(c) > 0xFFFF, e.g. rare CJK Extension B/C/D or emoji)
    occupy 2 UTF-16 code units (surrogate pair) in JavaScript.
    """
    clamped = max(0, min(codepoint_offset, len(text)))
    prefix = text[:clamped]
    return len(prefix.encode("utf-16-le")) // 2


def codepoint_span_to_utf16(text: str, start_offset: int, end_offset: int) -> tuple[int, int]:
    return (
        codepoint_to_utf16_offset(text, start_offset),
        codepoint_to_utf16_offset(text, end_offset),
    )
