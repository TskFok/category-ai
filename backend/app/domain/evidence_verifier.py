from dataclasses import dataclass

from app.domain.enums import VerificationStatus
from app.domain.text_processing import sha256_text


@dataclass(frozen=True)
class EvidenceVerificationResult:
    status: VerificationStatus
    resolved_start: int
    resolved_end: int
    matched_text: str
    text_hash: str
    reason: str | None = None


def verify_evidence_span(
    chapter_text: str,
    start_offset: int,
    end_offset: int,
    quote_text: str,
) -> EvidenceVerificationResult:
    """
    Strictly verify that quote_text matches chapter_text[start_offset:end_offset].
    If offsets are slightly shifted, attempts a unique exact-substring relocation;
    if multiple occurrences exist, marks as AMBIGUOUS for human review.
    """
    text_len = len(chapter_text)
    if start_offset < 0 or end_offset > text_len or start_offset >= end_offset:
        # Attempt unique relocation if quote_text is provided
        if quote_text:
            return _try_relocate(chapter_text, quote_text, VerificationStatus.OUT_OF_RANGE)
        return EvidenceVerificationResult(
            status=VerificationStatus.OUT_OF_RANGE,
            resolved_start=start_offset,
            resolved_end=end_offset,
            matched_text="",
            text_hash=sha256_text(""),
            reason="Offsets out of chapter bounds",
        )

    actual = chapter_text[start_offset:end_offset]
    if actual == quote_text:
        return EvidenceVerificationResult(
            status=VerificationStatus.VERIFIED,
            resolved_start=start_offset,
            resolved_end=end_offset,
            matched_text=actual,
            text_hash=sha256_text(actual),
        )

    if quote_text:
        return _try_relocate(chapter_text, quote_text, VerificationStatus.MISMATCH)

    return EvidenceVerificationResult(
        status=VerificationStatus.MISMATCH,
        resolved_start=start_offset,
        resolved_end=end_offset,
        matched_text=actual,
        text_hash=sha256_text(actual),
        reason="Empty quote_text",
    )


def _try_relocate(
    chapter_text: str,
    quote_text: str,
    fallback_status: VerificationStatus,
) -> EvidenceVerificationResult:
    first = chapter_text.find(quote_text)
    if first == -1:
        return EvidenceVerificationResult(
            status=fallback_status,
            resolved_start=0,
            resolved_end=0,
            matched_text="",
            text_hash=sha256_text(""),
            reason="Quote text not found in normalized chapter text",
        )
    second = chapter_text.find(quote_text, first + 1)
    if second != -1:
        return EvidenceVerificationResult(
            status=VerificationStatus.AMBIGUOUS,
            resolved_start=first,
            resolved_end=first + len(quote_text),
            matched_text=quote_text,
            text_hash=sha256_text(quote_text),
            reason="Quote text appears multiple times in chapter; requires review",
        )
    return EvidenceVerificationResult(
        status=VerificationStatus.VERIFIED,
        resolved_start=first,
        resolved_end=first + len(quote_text),
        matched_text=quote_text,
        text_hash=sha256_text(quote_text),
        reason="Relocated uniquely within chapter",
    )
