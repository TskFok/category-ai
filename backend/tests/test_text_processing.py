from app.domain.text_processing import (
    chunk_chapter_text,
    codepoint_span_to_utf16,
    detect_and_decode,
    parse_chapters,
)


def test_parse_chapters_and_diagnostics():
    raw = (
        "第一章 初入仙界\n"
        "韩立睁开双眼，发现自己身处一座陌生的仙城。\n\n"
        "第三章 奇遇\n"
        "高升与柳乐儿在酒馆中交谈。\n"
    )
    chapters, diag = parse_chapters(raw)
    assert len(chapters) == 2
    assert chapters[0].chapter_no == 1
    assert chapters[0].declared_no == 1
    assert chapters[1].chapter_no == 2
    assert chapters[1].declared_no == 3
    # Chapter 2 was skipped in headings -> detected in diagnostics
    assert diag.missing_chapter_numbers == [2]
    assert diag.total_chars == sum(c.char_count for c in chapters)


def test_detect_gb18030_encoding():
    text = "第一章 凡人修仙\n韩立在山谷中打坐修炼。"
    raw_bytes = text.encode("gb18030")
    decoded, enc = detect_and_decode(raw_bytes)
    assert "韩立" in decoded
    assert enc in ("gb18030", "gbk", "cp936")


def test_chunk_chapter_overlapping_offsets():
    # Build a 1000-char chapter and chunk into 400 with 100 overlap
    chapter = ("韩立在洞府中参悟时间法则，周围灵气涌动。\n" * 50).strip()
    chunks = chunk_chapter_text(chapter, chunk_size=400, overlap=100)
    assert len(chunks) >= 3
    for idx, ch in enumerate(chunks):
        assert chapter[ch.start_offset : ch.end_offset] == ch.text
        if idx > 0:
            # Must overlap with previous chunk
            assert ch.start_offset < chunks[idx - 1].end_offset
    assert chunks[0].start_offset == 0
    assert chunks[-1].end_offset == len(chapter)


def test_utf16_surrogate_pair_conversion():
    # U+20000 (𠀀) is a CJK Extension B character outside the BMP (1 Python codepoint, 2 UTF-16 code units)
    text = "韩立𠀀道友"
    assert len(text) == 5
    # "道友" is at codepoint [3, 5), which is UTF-16 [4, 6)
    u16_start, u16_end = codepoint_span_to_utf16(text, 3, 5)
    assert (u16_start, u16_end) == (4, 6)
