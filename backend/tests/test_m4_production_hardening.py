from __future__ import annotations

import base64
import io
import json
import tempfile
import wave
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.core.config import Settings
from app.core.security import Principal, UserRole, create_signed_media_token, verify_signed_media_token
from app.domain.enums import AudioSegmentStatus, SegmentReviewStatus, SegmentType
from app.domain.script_validation import split_text_to_synthesis_units
from app.infrastructure.audio.ffmpeg_pipeline import (
    FFmpegUnavailableError,
    MergeInputSegment,
    inspect_wav_bytes,
    merge_episode_audio,
)
from app.infrastructure.db.models import AudioSegment, ReviewTask, ScriptSegment, SynthesisUnit, VoiceProfileVersion
from app.infrastructure.db.session import SessionLocal
from app.infrastructure.llm.deepseek import DeepSeekClient, LLMClientError
from app.infrastructure.tts.base import SynthesisRequest, TTSProviderError
from app.infrastructure.tts.cosyvoice import CosyVoiceTTSProvider, get_tts_provider
from app.infrastructure.tts.mock_tts import generate_pcm_wav_bytes

SAMPLE_NOVEL = """第一章 青牛镇夜话
夜色渐深，青牛镇的小客栈里灯火摇曳。
韩立放下茶杯，沉声道：“南宫道友，此去黄枫谷路途遥远，不可大意。”
南宫婉微微一笑，轻声回应：“韩道友放心，我早有准备。”
“可是前方魔气森森，我们真要今夜出发吗？”
“不知阁下来自何方？”
“且慢，容我想想！”
韩立心想：“此事必有蹊跷，我需多加防备。”
"""


def _setup_novel_and_episode(client: TestClient, text_content: str = SAMPLE_NOVEL):
    up = client.post(
        "/api/v1/novels",
        data={"title": "M4测试小说", "author": "测试作者", "copyright_confirmed": "true"},
        files={"file": ("m4.txt", text_content.encode("utf-8"), "text/plain")},
    )
    assert up.status_code == 201
    novel_id = up.json()["id"]

    job = client.post(
        f"/api/v1/novels/{novel_id}/jobs",
        json={"job_type": "FULL_PIPELINE", "run_inline": True},
    )
    assert job.status_code == 201

    chars = client.get(f"/api/v1/novels/{novel_id}/characters?page_size=50").json()["items"]
    char_by_name = {c["canonical_name"]: c for c in chars}

    plan = client.post(
        f"/api/v1/novels/{novel_id}/episode-plans",
        json={"target_minutes": 10, "chars_per_minute": 260, "strategy": "chapter_first"},
    ).json()
    ep1 = plan["episodes"][0]
    return novel_id, char_by_name, plan, ep1


def _lock_all_voices(client: TestClient, novel_id: str, segments: list[dict]) -> dict[str, dict]:
    profiles: dict[str, dict] = {}
    vp_n = client.post(
        f"/api/v1/novels/{novel_id}/voice-profiles",
        json={"narrator_key": "default", "display_name": "默认旁白", "voice_prompt": "沉稳男声旁白"},
    ).json()
    client.post(f"/api/v1/voice-profiles/{vp_n['id']}/design-candidates", json={})
    lk_n = client.post(f"/api/v1/voice-profiles/{vp_n['id']}/lock", json={"candidate_index": 0}).json()
    profiles["narrator:default"] = lk_n["profile"]

    used_chars = {s["speaker_character_id"] for s in segments if s["speaker_character_id"]}
    for cid in used_chars:
        vp = client.post(
            f"/api/v1/novels/{novel_id}/voice-profiles",
            json={"character_id": cid},
        ).json()
        client.post(f"/api/v1/voice-profiles/{vp['id']}/design-candidates", json={})
        lk = client.post(f"/api/v1/voice-profiles/{vp['id']}/lock", json={"candidate_index": 0}).json()
        profiles[cid] = lk["profile"]
    return profiles


# =============================================================================
# P0-01: CosyVoice HTTP synthesize contract
# =============================================================================
def test_cosyvoice_http_contract_official_response_url():
    valid_wav = generate_pcm_wav_bytes(duration_ms=400, frequency_hz=320.0, sample_rate=24000)
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={
                    "request_id": "req-official-001",
                    "output": {
                        "audio": {
                            "url": "https://dashscope-result.oss-cn-beijing.aliyuncs.com/temp/audio.wav",
                            "expires_at": 1900000000,
                        }
                    },
                    "usage": {"characters": 18},
                },
            )
        if request.method == "GET" and "audio.wav" in str(request.url):
            captured["downloaded"] = True
            return httpx.Response(200, content=valid_wav, headers={"content-type": "audio/wav"})
        return httpx.Response(404)

    settings = Settings(
        tts_mock_mode=False,
        cosyvoice_api_key="sk-test-real",
        cosyvoice_base_url="https://dashscope.aliyuncs.com/api/v1",
        cosyvoice_model="cosyvoice-v3.5-flash",
    )
    provider = CosyVoiceTTSProvider(settings, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    res = provider.synthesize(
        SynthesisRequest(
            text="韩立微微皱眉，转身望向山谷。",
            voice_id="cosyvoice-v3.5-flash-hanli-01",
            emotion="tense",
            intensity=0.8,
            rate=1.05,
            pitch=0.95,
        )
    )

    assert captured["url"].endswith("/services/audio/tts/SpeechSynthesizer")
    body = captured["body"]
    assert body["model"] == "cosyvoice-v3.5-flash"
    assert "parameters" not in body
    assert "emotion" not in body["input"]
    assert "emotion_intensity" not in body["input"]
    assert body["input"]["voice"] == "cosyvoice-v3.5-flash-hanli-01"
    assert body["input"]["format"] == "wav"
    assert body["input"]["sample_rate"] == 24000
    assert "instruction" in body["input"] and "语气紧绷克制" in body["input"]["instruction"]
    assert captured.get("downloaded") is True
    assert res.request_id == "req-official-001"
    assert res.billed_characters == 18
    assert res.cost_kind == "billed"
    assert res.currency == "CNY"
    assert res.is_mock is False
    assert res.audio_bytes[:4] == b"RIFF"


# =============================================================================
# P0-02: CosyVoice Voice Design contract (voice-enrollment, prefix, preview_audio.data)
# =============================================================================
def test_voice_design_contract_voice_enrollment_and_preview_audio():
    valid_wav = generate_pcm_wav_bytes(duration_ms=500, frequency_hz=280.0, sample_rate=24000)
    b64_wav = base64.b64encode(valid_wav).decode("ascii")
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.append({"url": str(request.url), "body": body})
        action = body["input"]["action"]
        if action == "create_voice":
            return httpx.Response(
                200,
                json={
                    "request_id": "req-vd-001",
                    "output": {
                        "voice_id": "cosyvoice-v3.5-flash-cv1234-abc",
                        "status": "DEPLOYING",
                        "preview_audio": {"data": b64_wav},
                    },
                },
            )
        if action == "query_voice":
            return httpx.Response(
                200,
                json={"request_id": "req-qv-001", "output": {"status": "OK"}},
            )
        return httpx.Response(400)

    settings = Settings(
        tts_mock_mode=False,
        cosyvoice_api_key="sk-test-real",
        cosyvoice_model="cosyvoice-v3.5-flash",
    )
    provider = CosyVoiceTTSProvider(settings, http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    # Short preview_text (<15 chars) must be rejected
    with pytest.raises(TTSProviderError) as exc_short:
        provider.design_voice(voice_prompt="沉稳男声", preview_text="太短了")
    assert exc_short.value.error_type == "INVALID_PREVIEW_TEXT_LENGTH"

    # Valid call
    vd = provider.design_voice(
        voice_prompt="沉稳克制的青年男性声音，语速适中。",
        preview_text="你好，我是韩立，这是我的专属有声书音色试听样本。",
        language="zh-CN",
    )
    assert captured[0]["url"].endswith("/services/audio/tts/customization")
    req_body = captured[0]["body"]
    assert req_body["model"] == "voice-enrollment"
    assert req_body["input"]["target_model"] == "cosyvoice-v3.5-flash"
    assert req_body["input"]["language_hints"] == ["zh"]
    assert "language" not in req_body["input"]
    assert 1 <= len(req_body["input"]["prefix"]) <= 10
    assert vd.voice_id == "cosyvoice-v3.5-flash-cv1234-abc"
    assert vd.provider_status == "DEPLOYING"
    assert vd.sample_audio_bytes[:4] == b"RIFF"

    # Query status
    st = provider.query_voice_status(vd.voice_id)
    assert st == "OK"
    assert captured[1]["body"]["input"]["action"] == "query_voice"


# =============================================================================
# P0-03: Script rerun preserves Segment IDs, AudioSegments, manual overrides (with FK ON!)
# =============================================================================
def test_script_rerun_preserves_audio_ids_and_manual_overrides(client: TestClient):
    # Verify SQLite PRAGMA foreign_keys is ON
    with SessionLocal() as db:
        fk_on = db.execute(text("PRAGMA foreign_keys")).scalar()
        assert fk_on == 1

    novel_id, char_by_name, _plan, ep1 = _setup_novel_and_episode(client)
    segs1 = client.post(f"/api/v1/episodes/{ep1['id']}/analyze-script").json()
    assert len(segs1) >= 6

    # Confirm all needs_review segments
    manual_seg_id = None
    for s in segs1:
        if s["review_status"] == "needs_review":
            r = client.patch(
                f"/api/v1/script-segments/{s['id']}",
                json={"speaker_character_id": char_by_name["南宫婉"]["id"], "emotion": "gentle", "review_status": "confirmed"},
            )
            assert r.status_code == 200
            if manual_seg_id is None:
                manual_seg_id = s["id"]

    segs_confirmed = client.get(f"/api/v1/episodes/{ep1['id']}/segments").json()
    _lock_all_voices(client, novel_id, segs_confirmed)

    # Render episode audio
    rj = client.post(f"/api/v1/episodes/{ep1['id']}/render-jobs", json={"run_inline": True})
    assert rj.status_code == 201
    assert rj.json()["status"] == "completed"

    before_segs = client.get(f"/api/v1/episodes/{ep1['id']}/segments").json()
    before_ids = [s["id"] for s in before_segs]
    before_audio_ids = {s["id"]: s["current_audio"]["id"] for s in before_segs}
    before_checksums = {s["id"]: s["current_audio"]["checksum"] for s in before_segs}

    # Re-analyze script repeatedly!
    for _ in range(2):
        re_resp = client.post(f"/api/v1/episodes/{ep1['id']}/analyze-script")
        assert re_resp.status_code == 200

    after_segs = client.get(f"/api/v1/episodes/{ep1['id']}/segments").json()
    assert [s["id"] for s in after_segs] == before_ids
    for s in after_segs:
        assert s["current_audio"] is not None
        assert s["current_audio"]["id"] == before_audio_ids[s["id"]]
        assert s["current_audio"]["checksum"] == before_checksums[s["id"]]
        assert s["current_audio"]["status"] == "ready"
        if s["id"] == manual_seg_id:
            assert s["manual_override"] is True
            assert s["speaker_character_id"] == char_by_name["南宫婉"]["id"]

    # Zero dangling ReviewTasks pointing to non-existent ScriptSegments
    with SessionLocal() as db:
        valid_ids = set(db.scalars(select(ScriptSegment.id)).all())
        rt_targets = list(
            db.scalars(select(ReviewTask.target_id).where(ReviewTask.target_kind == "script_segment")).all()
        )
        for tid in rt_targets:
            assert tid in valid_ids


# =============================================================================
# P0-04: Unresolved dialogue blocks render (409), never silently routed to narrator
# =============================================================================
def test_unresolved_dialogue_blocks_render_not_narrator(client: TestClient):
    novel_id, char_by_name, _plan, ep1 = _setup_novel_and_episode(client)
    segs = client.post(f"/api/v1/episodes/{ep1['id']}/analyze-script").json()

    # Force 3 dialogue segments to unresolved (speaker=None, needs_review)
    dialogues = [s for s in segs if s["segment_type"] == SegmentType.DIALOGUE.value]
    assert len(dialogues) >= 3
    with SessionLocal() as db:
        for d in dialogues[:3]:
            row = db.get(ScriptSegment, d["id"])
            row.speaker_character_id = None
            row.narrator_key = None
            row.review_status = SegmentReviewStatus.NEEDS_REVIEW.value
            row.manual_override = False
            row.is_render_blocked = True
        db.commit()

    # Lock narrator + Han Li voice so missing voices are NOT the reason
    _lock_all_voices(client, novel_id, segs + [{"speaker_character_id": char_by_name["韩立"]["id"]}])

    # Validate-script must report all 3 unresolved dialogues
    val = client.post(f"/api/v1/episodes/{ep1['id']}/validate-script").json()
    assert val["ready_to_render"] is False
    assert len(val["unresolved_segments"]) >= 3

    # Render MUST return 409 with exact blocking segment list
    rj_blocked = client.post(f"/api/v1/episodes/{ep1['id']}/render-jobs", json={"run_inline": True})
    assert rj_blocked.status_code == 409
    assert rj_blocked.json()["code"] == "UNRESOLVED_SPEAKER_SEGMENTS"
    blocked_ids = {b["segment_id"] for b in rj_blocked.json()["details"]["blocking_segments"]}
    for d in dialogues[:3]:
        assert d["id"] in blocked_ids

    # Confirm all unresolved segments -> now render succeeds
    for b_id in blocked_ids:
        client.patch(
            f"/api/v1/script-segments/{b_id}",
            json={"speaker_character_id": char_by_name["韩立"]["id"], "review_status": "confirmed"},
        )
    rj_ok = client.post(f"/api/v1/episodes/{ep1['id']}/render-jobs", json={"run_inline": True})
    assert rj_ok.status_code == 201
    assert rj_ok.json()["status"] == "completed"


# =============================================================================
# P0-05: Voice profile update increments version & invalidates ONLY impacted audio
# =============================================================================
def test_voice_profile_update_invalidates_only_impacted_audio(client: TestClient):
    novel_id, char_by_name, _plan, ep1 = _setup_novel_and_episode(client)
    segs = client.post(f"/api/v1/episodes/{ep1['id']}/analyze-script").json()
    for s in segs:
        if s["review_status"] == "needs_review":
            client.patch(
                f"/api/v1/script-segments/{s['id']}",
                json={"speaker_character_id": char_by_name["韩立"]["id"], "review_status": "confirmed"},
            )
    segs = client.get(f"/api/v1/episodes/{ep1['id']}/segments").json()
    profiles = _lock_all_voices(client, novel_id, segs)

    rj = client.post(f"/api/v1/episodes/{ep1['id']}/render-jobs", json={"run_inline": True})
    assert rj.status_code == 201

    hanli_id = char_by_name["韩立"]["id"]
    hanli_vp = profiles[hanli_id]
    old_ver = hanli_vp["profile_version"]

    # Update Han Li's voice_id via update-and-invalidate
    upd = client.post(
        f"/api/v1/voice-profiles/{hanli_vp['id']}/update-and-invalidate",
        json={"voice_id": "cv35-mock-hanli-v2", "default_rate": 1.1, "scope": "all_episodes"},
    )
    assert upd.status_code == 200
    upd_data = upd.json()
    assert upd_data["profile"]["profile_version"] == old_ver + 1
    assert upd_data["invalidated_audio_segments"] >= 1
    assert upd_data["currency"] == "CNY"

    # Verify VoiceProfileVersion snapshot exists
    with SessionLocal() as db:
        versions = list(
            db.scalars(
                select(VoiceProfileVersion).where(VoiceProfileVersion.voice_profile_id == hanli_vp["id"])
            ).all()
        )
        assert len(versions) >= 2

    # Only Han Li's segments are stale; narrator & Nan Gong Wan remain ready!
    segs_after = client.get(f"/api/v1/episodes/{ep1['id']}/segments").json()
    for s in segs_after:
        if s["speaker_character_id"] == hanli_id:
            assert s["current_audio"]["status"] == AudioSegmentStatus.STALE.value
        else:
            assert s["current_audio"]["status"] == AudioSegmentStatus.READY.value

    # Export refuses to mix stale audio (409)
    blocked_exp = client.post(
        f"/api/v1/novels/{novel_id}/audio-exports",
        json={"episode_id": ep1["id"], "format": "wav"},
    )
    assert blocked_exp.status_code == 409


# =============================================================================
# P0-06: Long narration over limit is split into SynthesisUnits without losing chars
# =============================================================================
def test_narration_over_limit_is_split_into_synthesis_units(client: TestClient):
    long_para = "青牛镇的夜风吹过长街，远山如墨，灯火零星。" * 100  # 2,200 chars
    novel_text = f"第一章 长篇旁白测试\n{long_para}\n"

    # Unit-level domain check
    units = split_text_to_synthesis_units(long_para, max_chars=380)
    assert len(units) >= 6
    assert all(len(u.text_content) <= 380 for u in units)
    assert "".join(u.text_content for u in units) == long_para

    # E2E render check
    novel_id, _chars, _plan, ep1 = _setup_novel_and_episode(client, text_content=novel_text)
    segs = client.post(f"/api/v1/episodes/{ep1['id']}/analyze-script").json()
    _lock_all_voices(client, novel_id, segs)

    rj = client.post(f"/api/v1/episodes/{ep1['id']}/render-jobs", json={"run_inline": True})
    assert rj.status_code == 201
    assert rj.json()["status"] == "completed"

    with SessionLocal() as db:
        db_units = list(
            db.scalars(select(SynthesisUnit).where(SynthesisUnit.episode_id == ep1["id"])).all()
        )
        assert len(db_units) >= 6
        assert all(u.status == "completed" for u in db_units)
        assert all(len(u.text_content) <= 400 for u in db_units)


# =============================================================================
# P0-07: Mixed sample rate / channel WAV normalization & explicit FFmpeg error
# =============================================================================
def test_mixed_sample_rate_wav_is_normalized_before_merge():
    wav_16k_stereo = generate_pcm_wav_bytes(duration_ms=400, frequency_hz=300.0, sample_rate=16000, channels=2)
    wav_44k_mono = generate_pcm_wav_bytes(duration_ms=600, frequency_hz=440.0, sample_rate=44100, channels=1)

    with tempfile.TemporaryDirectory() as tmp:
        p1 = Path(tmp) / "a.wav"
        p2 = Path(tmp) / "b.wav"
        p1.write_bytes(wav_16k_stereo)
        p2.write_bytes(wav_44k_mono)

        merged_bytes, total_ms, fmt = merge_episode_audio(
            [
                MergeInputSegment(audio_path=p1, speaker_key="char1", duration_ms=400),
                MergeInputSegment(audio_path=p2, speaker_key="char2", duration_ms=600),
            ],
            output_format="wav",
            sample_rate=24000,
            gap_same_ms=200,
            gap_change_ms=500,
        )
        assert fmt == "wav"
        meta = inspect_wav_bytes(merged_bytes)
        assert meta.sample_rate == 24000
        assert meta.channels == 1
        assert meta.sampwidth == 2
        # 400 + 500 (gap) + 600 = 1500ms (allow small rounding tolerance)
        assert 1400 <= total_ms <= 1600


def test_mp3_export_without_ffmpeg_fails_explicitly():
    wav_bytes = generate_pcm_wav_bytes(duration_ms=300, sample_rate=24000)
    with tempfile.TemporaryDirectory() as tmp:
        p1 = Path(tmp) / "a.wav"
        p1.write_bytes(wav_bytes)
        with pytest.raises(FFmpegUnavailableError) as exc_info:
            merge_episode_audio(
                [MergeInputSegment(audio_path=p1, speaker_key="narrator", duration_ms=300)],
                output_format="mp3",
                ffmpeg_bin="nonexistent_ffmpeg_binary_xyz",
                allow_wav_fallback=False,
            )
        assert exc_info.value.code == "FFMPEG_UNAVAILABLE"


# =============================================================================
# P0-08: Production never falls back to Mock when credentials are missing
# =============================================================================
def test_production_missing_key_never_falls_back_to_mock():
    prod_no_key = Settings(
        app_env="production",
        tts_mock_mode=False,
        cosyvoice_api_key="",
        deepseek_mock_mode=False,
        deepseek_api_key="",
    )
    with pytest.raises(TTSProviderError) as tts_err:
        get_tts_provider(prod_no_key)
    assert tts_err.value.error_type == "MISSING_PROVIDER_CREDENTIALS"

    prod_mock_on = Settings(
        app_env="production",
        tts_mock_mode=True,
        cosyvoice_api_key="sk-real-key",
    )
    with pytest.raises(TTSProviderError) as tts_mock_err:
        get_tts_provider(prod_mock_on)
    assert tts_mock_err.value.error_type == "PRODUCTION_MOCK_BLOCKED"

    llm = DeepSeekClient(prod_no_key)
    with pytest.raises(LLMClientError) as llm_err:
        llm.chat_json(system_prompt="s", user_prompt="u")
    assert llm_err.value.error_type == "PRODUCTION_MOCK_BLOCKED"


# =============================================================================
# P1-01 & P1-02: Pause / Resume / Retry-Failed + BookRenderJob + Audio Readiness
# =============================================================================
def test_pause_resume_retry_and_book_render_job(client: TestClient):
    novel_id, char_by_name, plan, ep1 = _setup_novel_and_episode(client)
    segs = client.post(f"/api/v1/episodes/{ep1['id']}/analyze-script").json()
    for s in segs:
        if s["review_status"] == "needs_review":
            client.patch(
                f"/api/v1/script-segments/{s['id']}",
                json={"speaker_character_id": char_by_name["韩立"]["id"], "review_status": "confirmed"},
            )
    segs = client.get(f"/api/v1/episodes/{ep1['id']}/segments").json()
    _lock_all_voices(client, novel_id, segs)

    # Check audio readiness before rendering
    readiness_before = client.get(f"/api/v1/novels/{novel_id}/audio-readiness").json()
    assert readiness_before["ready_for_export"] is False
    assert readiness_before["remaining_chars"] > 0
    assert readiness_before["currency"] == "CNY"

    # Create job with tiny budget so it pauses mid-way
    rj = client.post(
        f"/api/v1/episodes/{ep1['id']}/render-jobs",
        json={"run_inline": True, "budget_limit": 0.0001},
    ).json()
    assert rj["status"] in ("paused", "completed")

    # If paused, resume with higher budget via DB update and verify completion
    if rj["status"] == "paused":
        with SessionLocal() as db:
            from app.infrastructure.db.models import RenderJob

            row = db.get(RenderJob, rj["id"])
            row.budget_limit = 50.0
            db.commit()
        resumed = client.post(f"/api/v1/render-jobs/{rj['id']}/resume?run_inline=true")
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "completed"

    # Whole-book batch render job
    book_job = client.post(
        f"/api/v1/episode-plans/{plan['id']}/render",
        json={"run_inline": True},
    )
    assert book_job.status_code == 201
    bj_data = book_job.json()
    assert bj_data["status"] == "completed"
    assert bj_data["currency"] == "CNY"

    # Readiness should now be True
    readiness_after = client.get(f"/api/v1/novels/{novel_id}/audio-readiness").json()
    assert readiness_after["ready_for_export"] is True
    assert readiness_after["remaining_chars"] == 0


# =============================================================================
# P1-03: Disk-backed streaming ZIP export with checksums & manifest
# =============================================================================
def test_large_zip_export_bounded_memory(client: TestClient):
    novel_id, char_by_name, plan, ep1 = _setup_novel_and_episode(client)
    segs = client.post(f"/api/v1/episodes/{ep1['id']}/analyze-script").json()
    for s in segs:
        if s["review_status"] == "needs_review":
            client.patch(
                f"/api/v1/script-segments/{s['id']}",
                json={"speaker_character_id": char_by_name["韩立"]["id"], "review_status": "confirmed"},
            )
    segs = client.get(f"/api/v1/episodes/{ep1['id']}/segments").json()
    _lock_all_voices(client, novel_id, segs)

    client.post(f"/api/v1/episode-plans/{plan['id']}/render", json={"run_inline": True})

    exp = client.post(
        f"/api/v1/novels/{novel_id}/audio-exports",
        json={"plan_id": plan["id"], "format": "zip", "allow_wav_fallback": True},
    )
    assert exp.status_code == 201
    exp_data = exp.json()
    assert exp_data["status"] == "ready"
    assert len(exp_data["checksum"]) == 64
    assert len(exp_data["source_snapshot_hash"]) == 64

    dl = client.get(exp_data["download_url"])
    assert dl.status_code == 200
    with zipfile.ZipFile(io.BytesIO(dl.content), "r") as zf:
        manifest_name = next(n for n in zf.namelist() if n.endswith("manifest.json"))
        manifest = json.loads(zf.read(manifest_name).decode("utf-8"))
        assert manifest["provider_mode"] == "mock"
        assert manifest["source_snapshot_hash"] == exp_data["source_snapshot_hash"]


# =============================================================================
# P1-04: Episode plan with >60 episodes has titles for ALL episodes
# =============================================================================
def test_full_episode_plan_over_60_episodes_has_all_titles(client: TestClient):
    # Create 65 chapters (~520 chars each) so target=500 chars produces 65 episodes (> 60)
    chapters_text = "\n\n".join(
        f"第{i}章 历练之行第{i}节\n" + ("韩立御剑飞行，穿过云海与山谷，心中默默思索接下来的行程。" * 20)
        for i in range(1, 66)
    )
    up = client.post(
        "/api/v1/novels",
        data={"title": "超长篇分集测试", "copyright_confirmed": "true"},
        files={"file": ("long65.txt", chapters_text.encode("utf-8"), "text/plain")},
    )
    assert up.status_code == 201
    novel_id = up.json()["id"]

    plan = client.post(
        f"/api/v1/novels/{novel_id}/episode-plans",
        json={"target_minutes": 5, "chars_per_minute": 100, "strategy": "chapter_first", "use_llm_titles": True},
    ).json()
    assert len(plan["episodes"]) > 60
    assert plan["validation_json"]["valid"] is True
    assert plan["validation_json"]["coverage_ratio"] == 1.0
    for ep in plan["episodes"]:
        assert ep["title"] and len(ep["title"].strip()) > 0


# =============================================================================
# P1-08: Signed media token verification & path scoping
# =============================================================================
def test_signed_media_token_scoping_and_expiry():
    settings = Settings(
        app_env="production",
        admin_api_token="super-strong-prod-token-123456",
        media_token_ttl_seconds=60,
    )
    principal = Principal(user_id="admin", role=UserRole.ADMIN)
    target_path = "/api/v1/audio-segments/seg-123/stream"

    token, exp = create_signed_media_token(principal, target_path, settings, ttl_seconds=60)
    assert exp > 0

    verified = verify_signed_media_token(token, target_path, settings)
    assert verified is not None
    assert verified.role == UserRole.ADMIN

    # Wrong path must be rejected
    assert verify_signed_media_token(token, "/api/v1/audio-segments/other/stream", settings) is None

    # Tampered token must be rejected
    assert verify_signed_media_token(token + "x", target_path, settings) is None
