from __future__ import annotations

import json
import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError
from app.domain.enums import EpisodePlanStatus, EpisodeStatus
from app.domain.episode_rules import (
    ChapterSpanInput,
    CoverageValidationResult,
    compute_episode_metrics,
    extract_episode_text_slices,
    plan_episodes_deterministic,
    validate_episode_plan_coverage,
)
from app.infrastructure.db.models import Chapter, Episode, EpisodePlan, LLMCall, Novel
from app.infrastructure.llm.deepseek import DeepSeekClient, LLMClientError
from app.infrastructure.storage.local import LocalStorage
from worker.pipelines.prompt_loader import load_prompt

logger = logging.getLogger(__name__)


def _load_chapter_inputs(db: Session, storage: LocalStorage, novel_id: str) -> list[ChapterSpanInput]:
    chapters = list(
        db.scalars(
            select(Chapter)
            .where(Chapter.novel_id == novel_id)
            .order_by(Chapter.chapter_no.asc())
        ).all()
    )
    if not chapters:
        raise AppError(code="NO_CHAPTERS", message="Novel has no chapters to plan", status_code=400)
    return [
        ChapterSpanInput(
            chapter_id=ch.id,
            chapter_no=ch.chapter_no,
            title=ch.title,
            content=storage.read_text(ch.content_ref),
        )
        for ch in chapters
    ]


def create_episode_plan(
    db: Session,
    storage: LocalStorage,
    settings: Settings,
    *,
    novel_id: str,
    target_minutes: int | None = None,
    chars_per_minute: int | None = None,
    strategy: str = "chapter_first",
    max_deviation_pct: int = 25,
    use_llm_titles: bool = True,
) -> tuple[EpisodePlan, list[Episode]]:
    novel = db.get(Novel, novel_id)
    if not novel:
        raise AppError(code="NOVEL_NOT_FOUND", message="Novel not found", status_code=404)

    eff_target_min = target_minutes or settings.episode_target_minutes
    eff_cpm = chars_per_minute or settings.episode_chars_per_minute

    chapter_inputs = _load_chapter_inputs(db, storage, novel_id)
    planned = plan_episodes_deterministic(
        chapter_inputs,
        target_minutes=eff_target_min,
        chars_per_minute=eff_cpm,
        strategy=strategy,
        max_deviation_pct=max_deviation_pct,
    )

    # Optionally enrich episode titles & rationales with DeepSeek without altering text/offsets
    title_overrides: dict[int, tuple[str, str]] = {}
    if use_llm_titles and planned:
        try:
            llm = DeepSeekClient(settings)
            spec = load_prompt("episode_planning")
            summary_payload = [
                {
                    "episode_no": p.episode_no,
                    "char_count": p.char_count,
                    "estimated_minutes": round(p.estimated_seconds / 60.0, 1),
                    "default_title": p.title,
                    "default_rationale": p.rationale,
                }
                for p in planned[:60]
            ]
            resp = llm.chat_json(
                system_prompt=spec.system_prompt,
                user_prompt=spec.user_template.format(
                    novel_title=novel.title,
                    target_minutes=eff_target_min,
                    episodes_summary_json=json.dumps(summary_payload, ensure_ascii=False),
                ),
                tier=spec.model_tier,
                temperature=spec.temperature,
                max_tokens=spec.max_tokens,
                task_hint="episode_planning",
            )
            db.add(
                LLMCall(
                    novel_id=novel.id,
                    stage="EPISODE_PLANNING",
                    model=resp.model,
                    prompt_version=spec.version,
                    input_tokens=resp.input_tokens,
                    output_tokens=resp.output_tokens,
                    estimated_cost=resp.estimated_cost,
                    latency_ms=resp.latency_ms,
                    status="ok",
                )
            )
            for item in resp.data.get("episodes", []):
                ep_no = item.get("episode_no")
                t = (item.get("title") or "").strip()
                r = (item.get("rationale") or "").strip()
                if isinstance(ep_no, int) and t:
                    title_overrides[ep_no] = (t[:255], r)
        except LLMClientError as exc:
            logger.warning("Episode title refinement skipped due to LLM error: %s", exc)

    max_ver = (
        db.scalar(
            select(func.coalesce(func.max(EpisodePlan.plan_version), 0)).where(
                EpisodePlan.novel_id == novel.id
            )
        )
        or 0
    )

    validation = validate_episode_plan_coverage(
        chapter_inputs,
        [
            (p.episode_no, p.start_chapter_id, p.start_offset, p.end_chapter_id, p.end_offset)
            for p in planned
        ],
    )

    plan = EpisodePlan(
        novel_id=novel.id,
        source_version=novel.source_version,
        plan_version=int(max_ver) + 1,
        target_minutes=eff_target_min,
        chars_per_minute=eff_cpm,
        strategy=strategy,
        max_deviation_pct=max_deviation_pct,
        status=EpisodePlanStatus.VALIDATED.value if validation.valid else EpisodePlanStatus.DRAFT.value,
        validation_json={
            "valid": validation.valid,
            "coverage_ratio": validation.coverage_ratio,
            "total_chars": validation.total_chars,
            "covered_chars": validation.covered_chars,
            "errors": validation.errors,
        },
    )
    db.add(plan)
    db.flush()

    episodes: list[Episode] = []
    for p in planned:
        ov_title, ov_rationale = title_overrides.get(p.episode_no, (p.title, p.rationale))
        ep = Episode(
            plan_id=plan.id,
            novel_id=novel.id,
            episode_no=p.episode_no,
            title=ov_title,
            rationale=ov_rationale or p.rationale,
            start_chapter_id=p.start_chapter_id,
            start_offset=p.start_offset,
            end_chapter_id=p.end_chapter_id,
            end_offset=p.end_offset,
            char_count=p.char_count,
            paragraph_count=p.paragraph_count,
            estimated_seconds=p.estimated_seconds,
            text_hash=p.text_hash,
            status=EpisodeStatus.DRAFT.value,
        )
        db.add(ep)
        episodes.append(ep)

    db.commit()
    db.refresh(plan)
    for ep in episodes:
        db.refresh(ep)
    return plan, episodes


def list_episode_plans(db: Session, novel_id: str) -> list[EpisodePlan]:
    if not db.get(Novel, novel_id):
        raise AppError(code="NOVEL_NOT_FOUND", message="Novel not found", status_code=404)
    return list(
        db.scalars(
            select(EpisodePlan)
            .where(EpisodePlan.novel_id == novel_id)
            .order_by(EpisodePlan.plan_version.desc())
        ).all()
    )


def get_episode_plan(db: Session, plan_id: str) -> EpisodePlan:
    plan = db.get(EpisodePlan, plan_id)
    if not plan:
        raise AppError(code="EPISODE_PLAN_NOT_FOUND", message="Episode plan not found", status_code=404)
    return plan


def list_episodes(db: Session, plan_id: str) -> list[Episode]:
    get_episode_plan(db, plan_id)
    return list(
        db.scalars(
            select(Episode).where(Episode.plan_id == plan_id).order_by(Episode.episode_no.asc())
        ).all()
    )


def get_episode(db: Session, episode_id: str) -> Episode:
    ep = db.get(Episode, episode_id)
    if not ep:
        raise AppError(code="EPISODE_NOT_FOUND", message="Episode not found", status_code=404)
    return ep


def validate_episode_plan(
    db: Session,
    storage: LocalStorage,
    plan_id: str,
) -> tuple[EpisodePlan, CoverageValidationResult]:
    plan = get_episode_plan(db, plan_id)
    episodes = list_episodes(db, plan_id)
    chapter_inputs = _load_chapter_inputs(db, storage, plan.novel_id)

    res = validate_episode_plan_coverage(
        chapter_inputs,
        [
            (e.episode_no, e.start_chapter_id, e.start_offset, e.end_chapter_id, e.end_offset)
            for e in episodes
        ],
    )
    plan.validation_json = {
        "valid": res.valid,
        "coverage_ratio": res.coverage_ratio,
        "total_chars": res.total_chars,
        "covered_chars": res.covered_chars,
        "errors": res.errors,
    }
    plan.status = EpisodePlanStatus.VALIDATED.value if res.valid else EpisodePlanStatus.DRAFT.value
    db.commit()
    db.refresh(plan)
    return plan, res


def update_episode(
    db: Session,
    storage: LocalStorage,
    episode_id: str,
    *,
    title: str | None = None,
    rationale: str | None = None,
    start_chapter_id: str | None = None,
    start_offset: int | None = None,
    end_chapter_id: str | None = None,
    end_offset: int | None = None,
    status: str | None = None,
) -> Episode:
    ep = get_episode(db, episode_id)
    plan = get_episode_plan(db, ep.plan_id)

    if title is not None:
        ep.title = title.strip()[:255] or ep.title
    if rationale is not None:
        ep.rationale = rationale
    if status is not None and status in {s.value for s in EpisodeStatus}:
        ep.status = status

    boundary_changed = any(
        v is not None for v in (start_chapter_id, start_offset, end_chapter_id, end_offset)
    )
    if boundary_changed:
        new_sc = start_chapter_id or ep.start_chapter_id
        new_so = start_offset if start_offset is not None else ep.start_offset
        new_ec = end_chapter_id or ep.end_chapter_id
        new_eo = end_offset if end_offset is not None else ep.end_offset

        chapter_inputs = _load_chapter_inputs(db, storage, ep.novel_id)
        try:
            slices = extract_episode_text_slices(chapter_inputs, new_sc, new_so, new_ec, new_eo)
        except ValueError as exc:
            raise AppError(code="INVALID_EPISODE_BOUNDARY", message=str(exc), status_code=400) from exc

        char_count, para_count, est_sec, text_hash = compute_episode_metrics(
            slices, plan.chars_per_minute
        )
        ep.start_chapter_id = new_sc
        ep.start_offset = new_so
        ep.end_chapter_id = new_ec
        ep.end_offset = new_eo
        ep.char_count = char_count
        ep.paragraph_count = para_count
        ep.estimated_seconds = est_sec
        ep.text_hash = text_hash

    db.commit()
    if boundary_changed:
        validate_episode_plan(db, storage, ep.plan_id)
    db.refresh(ep)
    return ep
