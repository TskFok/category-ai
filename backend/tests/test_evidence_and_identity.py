from app.domain.enums import VerificationStatus
from app.domain.evidence_verifier import verify_evidence_span
from app.domain.identity_rules import is_generic_honorific, resolve_by_rules


def test_verify_evidence_exact_and_relocation_and_ambiguous():
    chapter = "第一章\n韩立缓缓抬头，看向远方的南宫婉。不久后，韩立再次起身。"

    # Exact match
    pos = chapter.index("南宫婉")
    res = verify_evidence_span(chapter, pos, pos + 3, "南宫婉")
    assert res.status == VerificationStatus.VERIFIED
    assert res.resolved_start == pos
    assert res.resolved_end == pos + 3

    # Shifted offset, unique quote -> uniquely relocated
    res2 = verify_evidence_span(chapter, 0, 3, "南宫婉")
    assert res2.status == VerificationStatus.VERIFIED
    assert res2.resolved_start == pos

    # Quote appears twice with wrong offsets -> AMBIGUOUS
    res3 = verify_evidence_span(chapter, 0, 2, "韩立")
    assert res3.status == VerificationStatus.AMBIGUOUS

    # Non-existent quote -> MISMATCH
    res4 = verify_evidence_span(chapter, 0, 3, "不存在的文本")
    assert res4.status == VerificationStatus.MISMATCH


def test_identity_rules():
    assert is_generic_honorific("前辈") is True
    assert is_generic_honorific("韩立") is False

    canonical_map = {"韩立": ["c1"], "王林": ["c2", "c3"]}
    aliases = [
        ("c1", "韩道友", 1, 500),
        ("c1", "厉飞雨", 1, 100),
    ]

    # Generic honorific must not auto-bind
    cid, reason = resolve_by_rules("前辈", 10, canonical_map, aliases)
    assert cid is None
    assert reason == "generic_honorific_requires_context"

    # Unique canonical
    cid, reason = resolve_by_rules("韩立", 10, canonical_map, aliases)
    assert cid == "c1"
    assert reason == "canonical_exact"

    # Homonym canonical -> ambiguous
    cid, reason = resolve_by_rules("王林", 10, canonical_map, aliases)
    assert cid is None
    assert reason == "ambiguous_homonym_canonical"

    # Scoped alias within chapter range
    cid, reason = resolve_by_rules("厉飞雨", 50, canonical_map, aliases)
    assert cid == "c1"
    assert reason == "alias_scoped"

    # Scoped alias outside chapter range
    cid, reason = resolve_by_rules("厉飞雨", 250, canonical_map, aliases)
    assert cid is None
    assert reason == "no_rule_match"
