from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.text_processing import sha256_text


@dataclass(frozen=True)
class ChapterSpanInput:
    chapter_id: str
    chapter_no: int
    title: str
    content: str


@dataclass(frozen=True)
class PlannedEpisode:
    episode_no: int
    title: str
    rationale: str
    start_chapter_id: str
    start_offset: int
    end_chapter_id: str
    end_offset: int
    char_count: int
    paragraph_count: int
    estimated_seconds: int
    text_hash: str


@dataclass(frozen=True)
class EpisodeTextSlice:
    chapter_id: str
    chapter_no: int
    start_offset: int
    end_offset: int
    text: str


@dataclass(frozen=True)
class CoverageValidationResult:
    valid: bool
    coverage_ratio: float
    total_chars: int
    covered_chars: int
    errors: list[str] = field(default_factory=list)


def estimate_seconds_for_chars(char_count: int, chars_per_minute: int = 260) -> int:
    if char_count <= 0:
        return 0
    cpm = max(50, chars_per_minute)
    return max(1, int(round((char_count / cpm) * 60)))


def extract_episode_text_slices(
    chapters: list[ChapterSpanInput],
    start_chapter_id: str,
    start_offset: int,
    end_chapter_id: str,
    end_offset: int,
) -> list[EpisodeTextSlice]:
    """
    Resolve an episode's (start_chapter_id, start_offset) .. (end_chapter_id, end_offset)
    into exact chapter text slices without modifying original text.
    """
    idx_map = {ch.chapter_id: i for i, ch in enumerate(chapters)}
    if start_chapter_id not in idx_map or end_chapter_id not in idx_map:
        raise ValueError("Episode references unknown chapter_id")

    start_idx = idx_map[start_chapter_id]
    end_idx = idx_map[end_chapter_id]
    if start_idx > end_idx:
        raise ValueError("start_chapter_id appears after end_chapter_id")

    slices: list[EpisodeTextSlice] = []
    for i in range(start_idx, end_idx + 1):
        ch = chapters[i]
        s_off = start_offset if i == start_idx else 0
        e_off = end_offset if i == end_idx else len(ch.content)
        if s_off < 0 or e_off > len(ch.content) or s_off >= e_off:
            raise ValueError(
                f"Invalid offset range [{s_off}, {e_off}) for chapter {ch.chapter_no} (len={len(ch.content)})"
            )
        slices.append(
            EpisodeTextSlice(
                chapter_id=ch.chapter_id,
                chapter_no=ch.chapter_no,
                start_offset=s_off,
                end_offset=e_off,
                text=ch.content[s_off:e_off],
            )
        )
    return slices


def compute_episode_metrics(
    slices: list[EpisodeTextSlice],
    chars_per_minute: int = 260,
) -> tuple[int, int, int, str]:
    full_text = "\n".join(s.text for s in slices)
    char_count = sum(len(s.text) for s in slices)
    paragraph_count = sum(
        len([p for p in s.text.split("\n") if p.strip()]) for s in slices
    )
    est_seconds = estimate_seconds_for_chars(char_count, chars_per_minute)
    return char_count, paragraph_count, est_seconds, sha256_text(full_text)


def _split_chapter_offsets(content: str, target_chars: int, max_chars: int) -> list[tuple[int, int]]:
    """Split a single long chapter into contiguous [start, end) ranges at paragraph/sentence breaks."""
    n = len(content)
    if n <= max_chars:
        return [(0, n)]

    spans: list[tuple[int, int]] = []
    start = 0
    while start < n:
        remaining = n - start
        if remaining <= max_chars:
            spans.append((start, n))
            break

        ideal_end = min(start + target_chars, n)
        floor = start + int(target_chars * 0.65)
        ceil = min(start + max_chars, n)

        # Prefer paragraph break near ideal_end
        split_pos = content.rfind("\n", floor, ceil)
        if split_pos != -1 and split_pos > start:
            end = split_pos + 1
        else:
            # Fallback to sentence punctuation
            best_punct = -1
            for punct in ("。", "！", "？", "”"):
                pos = content.rfind(punct, floor, ceil)
                if pos > best_punct:
                    best_punct = pos
            end = best_punct + 1 if best_punct > start else ideal_end

        if end <= start:
            end = min(start + target_chars, n)
        spans.append((start, end))
        start = end

    return spans


def plan_episodes_deterministic(
    chapters: list[ChapterSpanInput],
    *,
    target_minutes: int = 25,
    chars_per_minute: int = 260,
    strategy: str = "chapter_first",
    max_deviation_pct: int = 25,
) -> list[PlannedEpisode]:
    """
    EP-01: Deterministically plan continuous, non-overlapping episodes covering 100%
    of normalized chapter text:
    - Merges short chapters until near target_chars.
    - Splits overly long chapters at natural paragraph boundaries.
    """
    if not chapters:
        return []

    target_chars = max(200, target_minutes * chars_per_minute)
    max_chars = int(target_chars * (1.0 + max(5, max_deviation_pct) / 100.0))

    # Build atomic units: (chapter, start_offset, end_offset)
    units: list[tuple[ChapterSpanInput, int, int]] = []
    for ch in chapters:
        if not ch.content:
            continue
        if len(ch.content) > max_chars:
            for s, e in _split_chapter_offsets(ch.content, target_chars, max_chars):
                units.append((ch, s, e))
        else:
            units.append((ch, 0, len(ch.content)))

    if not units:
        return []

    # Group atomic units into episodes
    grouped: list[list[tuple[ChapterSpanInput, int, int]]] = []
    current_group: list[tuple[ChapterSpanInput, int, int]] = []
    current_chars = 0

    for unit in units:
        unit_len = unit[2] - unit[1]
        if current_group and (current_chars + unit_len > max_chars):
            grouped.append(current_group)
            current_group = [unit]
            current_chars = unit_len
        else:
            current_group.append(unit)
            current_chars += unit_len
            if strategy == "chapter_first" and current_chars >= target_chars:
                grouped.append(current_group)
                current_group = []
                current_chars = 0

    if current_group:
        grouped.append(current_group)

    planned: list[PlannedEpisode] = []
    for ep_idx, group in enumerate(grouped, start=1):
        first_ch, first_start, _ = group[0]
        last_ch, _, last_end = group[-1]
        slices = extract_episode_text_slices(
            chapters, first_ch.chapter_id, first_start, last_ch.chapter_id, last_end
        )
        char_count, para_count, est_sec, text_hash = compute_episode_metrics(
            slices, chars_per_minute
        )

        if first_ch.chapter_id == last_ch.chapter_id:
            if first_start == 0 and last_end == len(first_ch.content):
                ep_title = f"第{ep_idx}集：{first_ch.title}"
                rationale = f"按原章节边界独立成集（{first_ch.title}，约{est_sec // 60}分钟）"
            else:
                ep_title = f"第{ep_idx}集：{first_ch.title}（片段）"
                rationale = f"长章节在自然段边界拆分（{first_ch.title} [{first_start}:{last_end}]）"
        else:
            ep_title = f"第{ep_idx}集：{first_ch.title} 至 {last_ch.title}"
            rationale = (
                f"合并第{first_ch.chapter_no}章至第{last_ch.chapter_no}章以贴近目标时长"
                f"（{target_minutes}分钟）"
            )

        planned.append(
            PlannedEpisode(
                episode_no=ep_idx,
                title=ep_title[:255],
                rationale=rationale,
                start_chapter_id=first_ch.chapter_id,
                start_offset=first_start,
                end_chapter_id=last_ch.chapter_id,
                end_offset=last_end,
                char_count=char_count,
                paragraph_count=para_count,
                estimated_seconds=est_sec,
                text_hash=text_hash,
            )
        )

    return planned


def validate_episode_plan_coverage(
    chapters: list[ChapterSpanInput],
    episodes: list[tuple[int, str, int, str, int]],
) -> CoverageValidationResult:
    """
    Verify that episodes form a 100% contiguous, non-overlapping cover of all chapters.
    Each tuple in `episodes`: (episode_no, start_chapter_id, start_offset, end_chapter_id, end_offset).
    """
    non_empty = [ch for ch in chapters if len(ch.content) > 0]
    total_chars = sum(len(ch.content) for ch in non_empty)
    if total_chars == 0:
        return CoverageValidationResult(
            valid=len(episodes) == 0,
            coverage_ratio=1.0,
            total_chars=0,
            covered_chars=0,
            errors=[],
        )

    if not episodes:
        return CoverageValidationResult(
            valid=False,
            coverage_ratio=0.0,
            total_chars=total_chars,
            covered_chars=0,
            errors=["Episode plan has no episodes"],
        )

    # Map every (chapter_id, offset) to a global linear offset in [0, total_chars]
    chapter_global_start: dict[str, int] = {}
    chapter_len: dict[str, int] = {}
    cursor = 0
    for ch in non_empty:
        chapter_global_start[ch.chapter_id] = cursor
        chapter_len[ch.chapter_id] = len(ch.content)
        cursor += len(ch.content)

    errors: list[str] = []
    linear_ranges: list[tuple[int, int, int]] = []  # (ep_no, g_start, g_end)

    for ep_no, s_cid, s_off, e_cid, e_off in sorted(episodes, key=lambda x: x[0]):
        if s_cid not in chapter_global_start or e_cid not in chapter_global_start:
            errors.append(f"Episode {ep_no}: references unknown chapter")
            continue
        if s_off < 0 or s_off > chapter_len[s_cid]:
            errors.append(f"Episode {ep_no}: start_offset {s_off} out of bounds")
            continue
        if e_off < 0 or e_off > chapter_len[e_cid]:
            errors.append(f"Episode {ep_no}: end_offset {e_off} out of bounds")
            continue
        g_start = chapter_global_start[s_cid] + s_off
        g_end = chapter_global_start[e_cid] + e_off
        if g_start >= g_end:
            errors.append(f"Episode {ep_no}: empty or reversed span [{g_start}, {g_end})")
            continue
        linear_ranges.append((ep_no, g_start, g_end))

    covered_chars = 0
    expected_pos = 0
    for ep_no, g_start, g_end in linear_ranges:
        if g_start < expected_pos:
            errors.append(
                f"Episode {ep_no}: overlaps previous episode (start={g_start} < expected={expected_pos})"
            )
        elif g_start > expected_pos:
            errors.append(
                f"Episode {ep_no}: gap of {g_start - expected_pos} chars before episode"
            )
        covered_chars += max(0, g_end - max(g_start, expected_pos))
        expected_pos = max(expected_pos, g_end)

    if expected_pos < total_chars:
        errors.append(f"Trailing uncovered text of {total_chars - expected_pos} chars at end of novel")

    coverage_ratio = round(covered_chars / total_chars, 6) if total_chars > 0 else 1.0
    return CoverageValidationResult(
        valid=(len(errors) == 0 and covered_chars == total_chars),
        coverage_ratio=coverage_ratio,
        total_chars=total_chars,
        covered_chars=covered_chars,
        errors=errors,
    )
