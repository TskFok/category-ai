from dataclasses import dataclass

# Generic honorifics/titles that must NOT be globally bound to one character without context
GENERIC_HONORIFICS: frozenset[str] = frozenset(
    {
        "前辈",
        "道友",
        "长老",
        "宗主",
        "阁主",
        "宫主",
        "师尊",
        "师父",
        "师兄",
        "师姐",
        "师弟",
        "师妹",
        "掌门",
        "仙子",
        "公子",
        "老祖",
        "大人",
        "少主",
        "城主",
        "族长",
    }
)


@dataclass(frozen=True)
class CandidateMatch:
    character_id: str
    match_kind: str  # "canonical_exact" | "alias_scoped"
    confidence: float


def is_generic_honorific(surface_text: str) -> bool:
    return surface_text.strip() in GENERIC_HONORIFICS


def resolve_by_rules(
    surface_text: str,
    chapter_no: int,
    canonical_map: dict[str, list[str]],
    alias_entries: list[tuple[str, str, int | None, int | None]],
) -> tuple[str | None, str]:
    """
    Rule-first identity resolution (Section 7.4):
    1. Reject generic honorifics from automatic global binding -> unresolved.
    2. Exact canonical name match when unambiguous (exactly 1 character).
    3. Confirmed alias match within valid chapter range when unambiguous.
    Returns (character_id_or_none, reason).
    """
    cleaned = surface_text.strip()
    if not cleaned:
        return None, "empty_surface_text"

    if is_generic_honorific(cleaned):
        return None, "generic_honorific_requires_context"

    # 1. Exact canonical name match
    canonical_hits = canonical_map.get(cleaned, [])
    if len(canonical_hits) == 1:
        return canonical_hits[0], "canonical_exact"
    if len(canonical_hits) > 1:
        return None, "ambiguous_homonym_canonical"

    # 2. Scoped alias match
    alias_hits: set[str] = set()
    for char_id, alias_text, valid_from, valid_to in alias_entries:
        if alias_text != cleaned:
            continue
        if valid_from is not None and chapter_no < valid_from:
            continue
        if valid_to is not None and chapter_no > valid_to:
            continue
        alias_hits.add(char_id)

    if len(alias_hits) == 1:
        return next(iter(alias_hits)), "alias_scoped"
    if len(alias_hits) > 1:
        return None, "ambiguous_alias"

    return None, "no_rule_match"
