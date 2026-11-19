from __future__ import annotations

import io
import json
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.infrastructure.tts.base import SynthesisRequest, TTSProviderError
from app.infrastructure.tts.cosyvoice import CosyVoiceTTSProvider
from app.infrastructure.tts.mock_tts import generate_pcm_wav_bytes

SAMPLE_AUDIOBOOK_NOVEL = """第一章 青牛镇夜话
夜色渐深，青牛镇的小客栈里灯火摇曳。
韩立放下茶杯，沉声道：“南宫道友，此去黄枫谷路途遥远，不可大意。”
南宫婉微微一笑，轻声回应：“韩道友放心，我早有准备。”
“可是前方魔气森森，我们真要今夜出发吗？”
韩立心想：“此事必有蹊跷，我需多加防备。”

第二章 山谷遇袭
清晨薄雾弥漫，山谷中忽然传来一阵冷笑。
魔光现身于巨石之上，厉声喝道：“韩立，今日便是你的死期！”
韩立神色不变，平静道：“阁下未免太自信了。”
"""


def test_full_audiobook_pipeline_e2e(client: TestClient):
    # 1. Upload novel & run character extraction
    up = client.post(
        "/api/v1/novels",
        data={"title": "凡人有声剧测试", "author": "测试作者", "copyright_confirmed": "true"},
        files={"file": ("audiobook.txt", SAMPLE_AUDIOBOOK_NOVEL.encode("utf-8"), "text/plain")},
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
    assert "韩立" in char_by_name
    assert "南宫婉" in char_by_name

    # 2. EP-01: Create Episode Plan & verify 100% coverage
    plan_resp = client.post(
        f"/api/v1/novels/{novel_id}/episode-plans",
        json={"target_minutes": 10, "chars_per_minute": 260, "strategy": "chapter_first"},
    )
    assert plan_resp.status_code == 201
    plan = plan_resp.json()
    assert plan["status"] == "validated"
    assert plan["validation_json"]["valid"] is True
    assert plan["validation_json"]["coverage_ratio"] == 1.0
    assert len(plan["episodes"]) >= 1
    ep1 = plan["episodes"][0]

    # Edit episode title & validate plan
    upd_ep = client.patch(f"/api/v1/episodes/{ep1['id']}", json={"title": "第1集：青牛镇夜话（修订）"})
    assert upd_ep.status_code == 200
    assert upd_ep.json()["title"] == "第1集：青牛镇夜话（修订）"

    val_resp = client.post(f"/api/v1/episode-plans/{plan['id']}/validate")
    assert val_resp.status_code == 200
    assert val_resp.json()["valid"] is True

    # 3. SCRIPT-01: Analyze script segments, verify types, low-confidence review & manual override protection
    seg_resp = client.post(f"/api/v1/episodes/{ep1['id']}/analyze-script")
    assert seg_resp.status_code == 200
    segments = seg_resp.json()
    assert len(segments) >= 6

    types = {s["segment_type"] for s in segments}
    assert "narration" in types
    assert "dialogue" in types
    assert "inner_monologue" in types
    assert "scene_heading" in types

    # Find an ambiguous/low-confidence dialogue segment (the unattributed question)
    ambiguous = [s for s in segments if s["review_status"] == "needs_review"]
    assert len(ambiguous) >= 1

    # Manually assign speaker & emotion to the ambiguous segment
    target_seg = ambiguous[0]
    patch_seg = client.patch(
        f"/api/v1/script-segments/{target_seg['id']}",
        json={
            "speaker_character_id": char_by_name["南宫婉"]["id"],
            "emotion": "tense",
            "speech_rate": 0.95,
            "review_status": "confirmed",
        },
    )
    assert patch_seg.status_code == 200
    assert patch_seg.json()["manual_override"] is True
    assert patch_seg.json()["speaker_character_id"] == char_by_name["南宫婉"]["id"]

    # Re-analyze episode script: manual_override MUST NOT be silently overwritten!
    re_segs = client.post(f"/api/v1/episodes/{ep1['id']}/analyze-script").json()
    matched_after = [
        s
        for s in re_segs
        if s["chapter_id"] == target_seg["chapter_id"]
        and s["start_offset"] == target_seg["start_offset"]
        and s["end_offset"] == target_seg["end_offset"]
    ]
    assert len(matched_after) == 1
    assert matched_after[0]["id"] == target_seg["id"]  # P0-03: Stable segment ID preserved!
    assert matched_after[0]["manual_override"] is True
    assert matched_after[0]["speaker_character_id"] == char_by_name["南宫婉"]["id"]

    # Resolve any remaining needs_review segments before testing locked-voice gate (P0-04)
    for rem in [s for s in re_segs if s["review_status"] == "needs_review"]:
        client.patch(
            f"/api/v1/script-segments/{rem['id']}",
            json={"speaker_character_id": char_by_name["韩立"]["id"], "review_status": "confirmed"},
        )
    re_segs = client.get(f"/api/v1/episodes/{ep1['id']}/segments").json()

    # 4. VOICE-01: Render blocked before voices are locked
    blocked_render = client.post(
        f"/api/v1/episodes/{ep1['id']}/render-jobs",
        json={"run_inline": True},
    )
    assert blocked_render.status_code == 400
    assert blocked_render.json()["code"] == "MISSING_LOCKED_VOICES"

    # Create narrator + all referenced characters' voice profiles, design candidates, preview & lock
    vp_narrator = client.post(
        f"/api/v1/novels/{novel_id}/voice-profiles",
        json={"narrator_key": "default", "display_name": "默认旁白", "voice_prompt": "沉稳磁性男声旁白"},
    ).json()
    designed_n = client.post(f"/api/v1/voice-profiles/{vp_narrator['id']}/design-candidates", json={}).json()
    assert len(designed_n["candidates_json"]) == 3

    # Stream preview WAV
    prev_resp = client.get(f"/api/v1/voice-profiles/{vp_narrator['id']}/preview?candidate_index=0")
    assert prev_resp.status_code == 200
    assert prev_resp.content[:4] == b"RIFF"

    lock_n = client.post(
        f"/api/v1/voice-profiles/{vp_narrator['id']}/lock",
        json={"candidate_index": 0},
    )
    assert lock_n.status_code == 200
    assert lock_n.json()["profile"]["approval_status"] == "locked"
    assert lock_n.json()["profile"]["voice_id"].startswith("cv35-mock-")

    # Lock voices for all characters present in the script
    used_char_ids = {s["speaker_character_id"] for s in re_segs if s["speaker_character_id"]}
    for cid in used_char_ids:
        vp = client.post(
            f"/api/v1/novels/{novel_id}/voice-profiles",
            json={"character_id": cid},
        ).json()
        client.post(f"/api/v1/voice-profiles/{vp['id']}/design-candidates", json={})
        lk = client.post(f"/api/v1/voice-profiles/{vp['id']}/lock", json={"candidate_index": 0})
        assert lk.status_code == 200

    # 5. AUDIO-01 & TTS-01: Render episode, verify caching on 2nd run, and partial regeneration
    rj1 = client.post(
        f"/api/v1/episodes/{ep1['id']}/render-jobs",
        json={"run_inline": True, "idempotency_key": "ep1-run-1"},
    )
    assert rj1.status_code == 201
    assert rj1.json()["status"] == "completed"
    assert rj1.json()["progress_json"]["failed_segments"] == 0

    # Idempotency check: same key returns same job
    rj1_dup = client.post(
        f"/api/v1/episodes/{ep1['id']}/render-jobs",
        json={"run_inline": True, "idempotency_key": "ep1-run-1"},
    )
    assert rj1_dup.json()["id"] == rj1.json()["id"]

    # Second render with different key hits 100% cache (zero new TTS cost)
    rj2 = client.post(
        f"/api/v1/episodes/{ep1['id']}/render-jobs",
        json={"run_inline": True, "idempotency_key": "ep1-run-2"},
    )
    assert rj2.status_code == 201
    assert rj2.json()["progress_json"]["cached_segments"] == rj2.json()["progress_json"]["total_segments"]
    assert rj2.json()["spent_cost"] == 0.0

    # Snapshot all segment audio checksums
    segs_rendered = client.get(f"/api/v1/episodes/{ep1['id']}/segments").json()
    checksums_before = {s["id"]: s["current_audio"]["checksum"] for s in segs_rendered}

    # Stream one segment's audio
    first_audio_id = segs_rendered[0]["current_audio"]["id"]
    stream_r = client.get(f"/api/v1/audio-segments/{first_audio_id}/stream")
    assert stream_r.status_code == 200
    assert stream_r.content[:4] == b"RIFF"

    # Modify ONLY segment[1]'s emotion -> ONLY segment[1] becomes stale; export is blocked (409)
    edited_seg_id = segs_rendered[1]["id"]
    client.patch(f"/api/v1/script-segments/{edited_seg_id}", json={"emotion": "angry"})
    blocked_exp = client.post(
        f"/api/v1/novels/{novel_id}/audio-exports",
        json={"episode_id": ep1["id"], "format": "wav"},
    )
    assert blocked_exp.status_code == 409
    assert blocked_exp.json()["code"] == "EXPORT_BLOCKED_INCOMPLETE_AUDIO"

    # Regenerate ONLY edited_seg_id -> other segments keep identical checksums
    regen = client.post(f"/api/v1/script-segments/{edited_seg_id}/regenerate")
    assert regen.status_code == 200
    assert regen.json()["status"] == "ready"

    segs_after = client.get(f"/api/v1/episodes/{ep1['id']}/segments").json()
    for s in segs_after:
        if s["id"] != edited_seg_id:
            assert s["current_audio"]["checksum"] == checksums_before[s["id"]]

    # 6. EXPORT-01: Export episode WAV & full-novel ZIP with manifest.json
    exp_wav = client.post(
        f"/api/v1/novels/{novel_id}/audio-exports",
        json={"episode_id": ep1["id"], "format": "wav"},
    )
    assert exp_wav.status_code == 201
    exp_wav_data = exp_wav.json()
    assert exp_wav_data["status"] == "ready"
    assert exp_wav_data["duration_ms"] > 0

    dl_wav = client.get(exp_wav_data["download_url"])
    assert dl_wav.status_code == 200
    assert dl_wav.content[:4] == b"RIFF"

    # Render any other episodes if present before ZIP export
    for ep in plan["episodes"][1:]:
        client.post(f"/api/v1/episodes/{ep['id']}/analyze-script")
        client.post(f"/api/v1/episodes/{ep['id']}/render-jobs", json={"run_inline": True})

    exp_zip = client.post(
        f"/api/v1/novels/{novel_id}/audio-exports",
        json={"plan_id": plan["id"], "format": "zip"},
    )
    assert exp_zip.status_code == 201
    dl_zip = client.get(exp_zip.json()["download_url"])
    assert dl_zip.status_code == 200
    with zipfile.ZipFile(io.BytesIO(dl_zip.content), "r") as zf:
        names = zf.namelist()
        assert any(n.endswith("manifest.json") for n in names)
        manifest_name = next(n for n in names if n.endswith("manifest.json"))
        manifest = json.loads(zf.read(manifest_name).decode("utf-8"))
        assert manifest["novel_id"] == novel_id
        assert len(manifest["episodes"]) == len(plan["episodes"])

    # 7. OPS-01: Provider usage summary tracks TTS calls and cost
    usage = client.get(f"/api/v1/novels/{novel_id}/provider-usage").json()
    assert usage["total_input_chars"] > 0
    assert usage["total_audio_duration_ms"] > 0


def test_tts01_cosyvoice_adapter_retries_429_and_empty_audio():
    valid_wav = generate_pcm_wav_bytes(duration_ms=300, frequency_hz=300.0)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="Rate limited")
        if calls["n"] == 2:
            return httpx.Response(200, content=b"", headers={"content-type": "audio/wav"})
        return httpx.Response(
            200,
            content=valid_wav,
            headers={"content-type": "audio/wav", "x-request-id": "req-ok-3"},
        )

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    settings = Settings(
        tts_mock_mode=False,
        cosyvoice_api_key="test-key",
        tts_max_retries=3,
    )
    provider = CosyVoiceTTSProvider(settings, http_client=http_client)
    res = provider.synthesize(SynthesisRequest(text="测试语音合成重试", voice_id="voice-1"))
    assert calls["n"] == 3
    assert res.attempts == 3
    assert res.request_id == "req-ok-3"
    assert res.audio_bytes[:4] == b"RIFF"

    # Non-retryable 400 fails immediately without retrying
    bad_calls = {"n": 0}

    def bad_handler(request: httpx.Request) -> httpx.Response:
        bad_calls["n"] += 1
        return httpx.Response(400, text="Bad request")

    bad_provider = CosyVoiceTTSProvider(settings, http_client=httpx.Client(transport=httpx.MockTransport(bad_handler)))
    with pytest.raises(TTSProviderError) as exc_info:
        bad_provider.synthesize(SynthesisRequest(text="错误测试", voice_id="voice-1"))
    assert exc_info.value.error_type == "HTTP_400"
    assert bad_calls["n"] == 1
