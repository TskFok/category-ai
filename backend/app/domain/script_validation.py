from __future__ import annotations

import re
from dataclasses import dataclass

from app.domain.enums import SegmentType
from app.domain.text_processing import CHAPTER_HEADING_RE, sha256_text

# Matches Chinese curly quotes “...”, corner quotes 「...」/『...』, or straight quotes "..."
QUOTE_SPAN_RE = re.compile(r"“[^”\n]+”|「[^」\n]+」|『[^』\n]+』|\"[^\"\n]+\"")
INNER_MONOLOGUE_CUES = ("心想", "暗道", "心中暗道", "心里想", "思忖", "暗自思量", "内心")


@dataclass(frozen=True)
class RawScriptSegment:
    chapter_id: str
    start_offset: int
    end_offset: int
    text_content: str
    text_hash: str
    segment_type: str


def split_chapter_slice_to_segments(
    chapter_id: str,
    base_offset: int,
    slice_text: str,
) -> list[RawScriptSegment]:
    """
    SCRIPT-01 & FIX-05:
    Deterministically split a chapter slice into ordered narration / dialogue /
    inner_monologue / scene_heading segments while preserving exact Unicode
    code-point offsets [start_offset, end_offset) relative to the chapter text.
    Never rewrites or omits non-whitespace text.
    """
    segments: list[RawScriptSegment] = []
    pos = 0
    total_len = len(slice_text)

    # Process line-by-line so paragraph structure and offsets remain exact
    for line_match in re.finditer(r"[^\n]+", slice_text):
        line_start = line_match.start()
        line_end = line_match.end()
        line_text = line_match.group(0)
        stripped = line_text.strip()
        if not stripped:
            continue

        lead_ws = len(line_text) - len(line_text.lstrip())
        trail_ws = len(line_text) - len(line_text.rstrip())
        eff_start = line_start + lead_ws
        eff_end = line_end - trail_ws
        eff_text = slice_text[eff_start:eff_end]

        # Scene / chapter heading check
        if len(eff_text) <= 80 and CHAPTER_HEADING_RE.match(eff_text):
            segments.append(
                RawScriptSegment(
                    chapter_id=chapter_id,
                    start_offset=base_offset + eff_start,
                    end_offset=base_offset + eff_end,
                    text_content=eff_text,
                    text_hash=sha256_text(eff_text),
                    segment_type=SegmentType.SCENE_HEADING.value,
                )
            )
            pos = eff_end
            continue

        # Split line around quoted speech spans
        cursor = eff_start
        for qm in QUOTE_SPAN_RE.finditer(slice_text, eff_start, eff_end):
            q_start, q_end = qm.start(), qm.end()
            if q_start > cursor:
                nar_raw = slice_text[cursor:q_start]
                n_lead = len(nar_raw) - len(nar_raw.lstrip())
                n_trail = len(nar_raw) - len(nar_raw.rstrip())
                ns = cursor + n_lead
                ne = q_start - n_trail
                if ns < ne:
                    nar_text = slice_text[ns:ne]
                    segments.append(
                        RawScriptSegment(
                            chapter_id=chapter_id,
                            start_offset=base_offset + ns,
                            end_offset=base_offset + ne,
                            text_content=nar_text,
                            text_hash=sha256_text(nar_text),
                            segment_type=SegmentType.NARRATION.value,
                        )
                    )

            preceding = slice_text[max(eff_start, q_start - 20) : q_start]
            seg_type = (
                SegmentType.INNER_MONOLOGUE.value
                if any(cue in preceding for cue in INNER_MONOLOGUE_CUES)
                else SegmentType.DIALOGUE.value
            )
            q_text = slice_text[q_start:q_end]
            segments.append(
                RawScriptSegment(
                    chapter_id=chapter_id,
                    start_offset=base_offset + q_start,
                    end_offset=base_offset + q_end,
                    text_content=q_text,
                    text_hash=sha256_text(q_text),
                    segment_type=seg_type,
                )
            )
            cursor = q_end

        if cursor < eff_end:
            tail_raw = slice_text[cursor:eff_end]
            t_lead = len(tail_raw) - len(tail_raw.lstrip())
            t_trail = len(tail_raw) - len(tail_raw.rstrip())
            ts = cursor + t_lead
            te = eff_end - t_trail
            if ts < te:
                tail_text = slice_text[ts:te]
                segments.append(
                    RawScriptSegment(
                        chapter_id=chapter_id,
                        start_offset=base_offset + ts,
                        end_offset=base_offset + te,
                        text_content=tail_text,
                        text_hash=sha256_text(tail_text),
                        segment_type=SegmentType.NARRATION.value,
                    )
                )
        pos = max(pos, eff_end)

    _ = total_len  # explicit clarity
    return segments


def verify_segments_against_chapters(
    chapter_texts: dict[str, str],
    segments: list[tuple[str, int, int, str, str]],
) -> tuple[bool, list[str]]:
    """
    Verify every segment tuple (chapter_id, start_offset, end_offset, text_content, text_hash)
    strictly matches authoritative chapter text with no overlaps per chapter.
    """
    errors: list[str] = []
    last_end_by_chapter: dict[str, int] = {}

    for idx, (cid, start_off, end_off, text_content, text_hash) in enumerate(segments):
        ch_text = chapter_texts.get(cid)
        if ch_text is None:
            errors.append(f"Segment {idx}: unknown chapter_id {cid}")
            continue
        if start_off < 0 or end_off > len(ch_text) or start_off >= end_off:
            errors.append(f"Segment {idx}: invalid span [{start_off}, {end_off})")
            continue
        actual = ch_text[start_off:end_off]
        if actual != text_content:
            errors.append(f"Segment {idx}: text mismatch against authoritative chapter text")
        if sha256_text(actual) != text_hash:
            errors.append(f"Segment {idx}: text_hash mismatch")
        prev_end = last_end_by_chapter.get(cid, 0)
        if start_off < prev_end:
            errors.append(
                f"Segment {idx}: overlaps previous segment in chapter {cid} ({start_off} < {prev_end})"
            )
        last_end_by_chapter[cid] = max(prev_end, end_off)

    return len(errors) == 0, errors


@dataclass(frozen=True)
class RawSynthesisUnit:
    unit_index: int
    start_offset: int
    end_offset: int
    text_content: str
    text_hash: str


_SENTENCE_BOUNDARY_RE = re.compile(r"[。！？；…!?;]+[”’」』\"'\)\）]?")
_CLAUSE_BOUNDARY_RE = re.compile(r"[，、：,:]+[”’」』\"']?")


def split_text_to_synthesis_units(text: str, max_chars: int) -> list[RawSynthesisUnit]:
    """
    P0-06: Deterministically split a ScriptSegment's text into non-overlapping
    TTS-safe SynthesisUnits <= max_chars without dropping or mutating any character.
    Prefers sentence boundaries, then clause boundaries, then whitespace, then hard split.
    """
    if max_chars < 1:
        raise ValueError("max_chars must be >= 1")
    total_len = len(text)
    if total_len == 0:
        return []
    if total_len <= max_chars:
        return [
            RawSynthesisUnit(
                unit_index=0,
                start_offset=0,
                end_offset=total_len,
                text_content=text,
                text_hash=sha256_text(text),
            )
        ]

    units: list[RawSynthesisUnit] = []
    cursor = 0
    idx = 0
    while cursor < total_len:
        remaining = total_len - cursor
        if remaining <= max_chars:
            chunk = text[cursor:total_len]
            units.append(
                RawSynthesisUnit(
                    unit_index=idx,
                    start_offset=cursor,
                    end_offset=total_len,
                    text_content=chunk,
                    text_hash=sha256_text(chunk),
                )
            )
            break

        window = text[cursor : cursor + max_chars]
        split_rel = -1

        sent_matches = list(_SENTENCE_BOUNDARY_RE.finditer(window))
        if sent_matches:
            split_rel = sent_matches[-1].end()

        if split_rel <= 0:
            clause_matches = list(_CLAUSE_BOUNDARY_RE.finditer(window))
            if clause_matches:
                split_rel = clause_matches[-1].end()

        if split_rel <= 0:
            ws_pos = window.rfind(" ")
            if ws_pos > 0:
                split_rel = ws_pos + 1

        if split_rel <= 0 or split_rel > max_chars:
            split_rel = max_chars

        end_pos = cursor + split_rel
        chunk = text[cursor:end_pos]
        units.append(
            RawSynthesisUnit(
                unit_index=idx,
                start_offset=cursor,
                end_offset=end_pos,
                text_content=chunk,
                text_hash=sha256_text(chunk),
            )
        )
        cursor = end_pos
        idx += 1

    return units
