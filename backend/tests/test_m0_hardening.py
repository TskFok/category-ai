from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.security import validate_production_auth_config
from app.domain.enums import AnalysisStatus, JobState, PipelineStage, TaskState
from app.infrastructure.db.models import (
    Character,
    Evidence,
    EvidenceLink,
    Fact,
    Job,
    Novel,
    TaskRun,
)
from app.infrastructure.db.session import SessionLocal
from app.infrastructure.llm.deepseek import DeepSeekClient, LLMClientError
from app.infrastructure.storage.local import extract_text_from_epub_with_meta
from worker.tasks.pipeline_tasks import execute_job


def _make_epub_bytes(with_spine: bool = True) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("OEBPS/ch_b.xhtml", "<html><body><h1>第一章 开头</h1><p>韩立走进客栈。</p></body></html>")
        zf.writestr("OEBPS/ch_a.xhtml", "<html><body><h1>第二章 后续</h1><p>南宫婉现身相助。</p></body></html>")
        if with_spine:
            zf.writestr(
                "META-INF/container.xml",
                """<?xml version="1.0"?>
                <container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
                  <rootfiles>
                    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
                  </rootfiles>
                </container>""",
            )
            # Deliberately put ch_b BEFORE ch_a in spine (opposite of alphabetical filename order)
            zf.writestr(
                "OEBPS/content.opf",
                """<?xml version="1.0" encoding="utf-8"?>
                <package xmlns="http://www.idpf.org/2007/opf" version="3.0">
                  <manifest>
                    <item id="item1" href="ch_b.xhtml" media-type="application/xhtml+xml"/>
                    <item id="item2" href="ch_a.xhtml" media-type="application/xhtml+xml"/>
                  </manifest>
                  <spine>
                    <itemref idref="item1"/>
                    <itemref idref="item2"/>
                  </spine>
                </package>""",
            )
    return buf.getvalue()


def test_fix05_epub_spine_order_and_fallback_warning():
    spine_epub = _make_epub_bytes(with_spine=True)
    text, meta = extract_text_from_epub_with_meta(spine_epub)
    assert meta["epub_spine_used"] is True
    assert meta["epub_order_warning"] is None
    assert text.index("第一章 开头") < text.index("第二章 后续")

    no_spine_epub = _make_epub_bytes(with_spine=False)
    text_fb, meta_fb = extract_text_from_epub_with_meta(no_spine_epub)
    assert meta_fb["epub_spine_used"] is False
    assert meta_fb["epub_order_warning"] is not None
    assert text_fb.index("第二章 后续") < text_fb.index("第一章 开头")


def test_fix04_production_auth_and_anti_role_spoofing(client: TestClient, monkeypatch):
    # Weak or empty token must block production startup
    with pytest.raises(RuntimeError):
        validate_production_auth_config(Settings(app_env="production", admin_api_token=""))
    with pytest.raises(RuntimeError):
        validate_production_auth_config(Settings(app_env="production", admin_api_token="short"))

    strong_admin = "prod-admin-token-1234567890"
    strong_reader = "prod-reader-token-1234567890"
    validate_production_auth_config(Settings(app_env="production", admin_api_token=strong_admin))

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ADMIN_API_TOKEN", strong_admin)
    monkeypatch.setenv("READER_API_TOKEN", strong_reader)
    get_settings.cache_clear()

    # Missing token -> 401
    r_unauth = client.get("/api/v1/novels")
    assert r_unauth.status_code == 401

    # Reader token with spoofed X-User-Role: admin -> MUST NOT escalate to admin (403 on admin endpoint)
    r_spoof = client.post(
        "/api/v1/novels",
        headers={
            "Authorization": f"Bearer {strong_reader}",
            "X-User-Role": "admin",
        },
        data={"title": "Spoof Test"},
        files={"file": ("t.txt", "第一章\n测试".encode("utf-8"), "text/plain")},
    )
    assert r_spoof.status_code == 403

    # Valid admin token -> 201
    r_ok = client.post(
        "/api/v1/novels",
        headers={"Authorization": f"Bearer {strong_admin}"},
        data={"title": "Auth OK", "license_scope": "private", "copyright_confirmed": "true"},
        files={"file": ("t.txt", "第一章\n韩立来到青元山。".encode("utf-8"), "text/plain")},
    )
    assert r_ok.status_code == 201
    assert r_ok.json()["copyright_confirmed"] is True

    get_settings.cache_clear()


def test_fix01_fix02_full_character_coverage_and_scoped_evidence(client: TestClient):
    novel_text = (
        "第一章 群英会\n"
        "韩立与南宫婉、蟹道人、魔光、石穿空、柳乐儿、高升、甘九真、轮回殿主、曲鳞、白素媛齐聚大殿。\n"
    )
    r = client.post(
        "/api/v1/novels",
        data={"title": "十人测试"},
        files={"file": ("ten.txt", novel_text.encode("utf-8"), "text/plain")},
    )
    assert r.status_code == 201
    novel_id = r.json()["id"]

    # Run full pipeline inline
    rj = client.post(
        f"/api/v1/novels/{novel_id}/jobs",
        json={"job_type": "FULL_PIPELINE", "run_inline": True},
    )
    assert rj.status_code == 201
    job = client.get(f"/api/v1/jobs/{rj.json()['id']}").json()
    assert job["state"] == JobState.COMPLETED.value

    # FIX-01: Verify >5 characters (all 11) were analyzed, not capped at 5
    chars = client.get(f"/api/v1/novels/{novel_id}/characters?page_size=50").json()["items"]
    assert len(chars) >= 10
    analyzed = [c for c in chars if c["analysis_status"] in (AnalysisStatus.ANALYZED.value, AnalysisStatus.NEEDS_REVIEW.value)]
    assert len(analyzed) == len(chars)

    # Incremental single-character S5 rerun increments generation_version only for that character
    target_char = chars[0]
    r_inc = client.post(
        f"/api/v1/novels/{novel_id}/jobs",
        json={"job_type": "S5_ANALYZE", "character_ids": [target_char["id"]], "run_inline": True},
    )
    assert r_inc.status_code == 201
    reports = client.get(f"/api/v1/characters/{target_char['id']}/analyses").json()
    assert len(reports) >= 2
    assert reports[0]["generation_version"] == 2

    # FIX-02: Verify character-scoped evidence does not pull unrelated characters' evidence
    db = SessionLocal()
    try:
        from worker.tasks.pipeline_tasks import _collect_character_context

        c_a = db.get(Character, chars[0]["id"])
        c_b = db.get(Character, chars[1]["id"])
        # Attach a unique fact + evidence exclusively to c_b
        ev_exclusive = Evidence(
            novel_id=novel_id,
            chapter_id=client.get(f"/api/v1/novels/{novel_id}/chapters").json()["items"][0]["id"],
            start_offset=0,
            end_offset=5,
            quote_text="EXCLUSIVE_B_QUOTE",
            text_hash="hash_b",
            verification_status="verified",
        )
        db.add(ev_exclusive)
        db.flush()
        fact_b = Fact(
            novel_id=novel_id,
            subject_id=c_b.id,
            predicate="secret",
            value_json={"value": "only_b"},
        )
        db.add(fact_b)
        db.flush()
        db.add(
            EvidenceLink(
                novel_id=novel_id,
                evidence_id=ev_exclusive.id,
                subject_kind="fact",
                subject_id=fact_b.id,
            )
        )
        db.commit()

        _, _, _, _, evs_a = _collect_character_context(db, novel_id, c_a)
        _, _, _, _, evs_b = _collect_character_context(db, novel_id, c_b)
        assert all(e.quote_text != "EXCLUSIVE_B_QUOTE" for e in evs_a)
        assert any(e.quote_text == "EXCLUSIVE_B_QUOTE" for e in evs_b)
    finally:
        db.close()


def test_fix03_dag_stage_failure_blocks_publish_and_supports_resume(client: TestClient):
    novel_text = "第一章 测试\n韩立与南宫婉在山谷中相遇。\n"
    r = client.post(
        "/api/v1/novels",
        data={"title": "DAG测试"},
        files={"file": ("dag.txt", novel_text.encode("utf-8"), "text/plain")},
    )
    novel_id = r.json()["id"]
    initial_rev = r.json()["data_revision"]

    # Create job without running yet via direct service
    db = SessionLocal()
    try:
        from app.application.job_service import create_analysis_job

        job = create_analysis_job(
            db, get_settings(), novel_id=novel_id, job_type="FULL_PIPELINE", requested_by="test"
        )
        job_id = job.id
        # Verify S3-S6 TaskRuns are persisted upfront
        stages = {
            tr.stage
            for tr in db.query(TaskRun).filter(TaskRun.job_id == job_id).all()
        }
        assert PipelineStage.S3_DISAMBIGUATE.value in stages
        assert PipelineStage.S5_ANALYZE.value in stages
        assert PipelineStage.S6_PUBLISH.value in stages
    finally:
        db.close()

    # Inject network failure during S5_ANALYZE
    orig_chat = DeepSeekClient.chat_json

    def flaky_chat(self, *args, **kwargs):
        if kwargs.get("task_hint") == "analyze_character_full":
            raise LLMClientError("NETWORK_ERROR", "Simulated S5 timeout")
        return orig_chat(self, *args, **kwargs)

    with patch.object(DeepSeekClient, "chat_json", flaky_chat):
        db = SessionLocal()
        try:
            execute_job(db, job_id)
            job_after = db.get(Job, job_id)
            assert job_after.state == JobState.FAILED.value
            s6_run = (
                db.query(TaskRun)
                .filter(TaskRun.job_id == job_id, TaskRun.stage == PipelineStage.S6_PUBLISH.value)
                .first()
            )
            assert s6_run.state == TaskState.SKIPPED.value
            novel_after = db.get(Novel, novel_id)
            assert novel_after.data_revision == initial_rev  # Publish gate held!
        finally:
            db.close()

    # Resume without injected failure -> only failed/skipped stages rerun and succeed
    res_resume = client.post(f"/api/v1/jobs/{job_id}/resume")
    assert res_resume.status_code == 200
    job_final = client.get(f"/api/v1/jobs/{job_id}").json()
    assert job_final["state"] == JobState.COMPLETED.value
    novel_final = client.get(f"/api/v1/novels/{novel_id}").json()
    assert novel_final["data_revision"] == initial_rev + 1


def test_fix06_alembic_incremental_upgrade_and_downgrade():
    backend_dir = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="ncke_alembic_") as tmpdir:
        db_url = f"sqlite:///{tmpdir}/mig.db"
        env = {**os.environ, "DATABASE_URL": db_url}
        alembic_bin = str(Path(sys.executable).parent / "alembic")

        up = subprocess.run([alembic_bin, "upgrade", "head"], cwd=backend_dir, env=env, capture_output=True, text=True)
        assert up.returncode == 0, f"alembic upgrade failed: {up.stderr}"

        down = subprocess.run(
            [alembic_bin, "downgrade", "0001_initial_schema"],
            cwd=backend_dir,
            env=env,
            capture_output=True,
            text=True,
        )
        assert down.returncode == 0, f"alembic downgrade failed: {down.stderr}"

        re_up = subprocess.run(
            [alembic_bin, "upgrade", "head"], cwd=backend_dir, env=env, capture_output=True, text=True
        )
        assert re_up.returncode == 0, f"alembic re-upgrade failed: {re_up.stderr}"
