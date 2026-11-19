from fastapi.testclient import TestClient

SAMPLE_NOVEL = (
    "第一章 忘忧酒馆\n"
    "韩立坐在忘忧酒馆的角落，静静听着高升与柳乐儿谈论仙界秘闻。\n"
    "不久后，蟹道人从门外走入，向韩立点头致意。\n\n"
    "第二章 再遇故人\n"
    "韩立离开酒馆后，在坊市偶遇石穿空与南宫婉。三人商议前往秘境探查。\n"
).encode("utf-8")


def test_full_end_to_end_workflow(client: TestClient):
    # 1. Health check
    assert client.get("/api/v1/health/live").status_code == 200
    assert client.get("/api/v1/health/ready").status_code == 200

    # 2. Reject PDF upload
    bad = client.post(
        "/api/v1/novels",
        files={"file": ("book.pdf", b"%PDF-1.4...", "application/pdf")},
        data={"title": "PDF测试"},
    )
    assert bad.status_code == 400
    assert bad.json()["code"] == "UNSUPPORTED_PDF"

    # 3. Upload TXT novel
    up = client.post(
        "/api/v1/novels",
        files={"file": ("fanren.txt", SAMPLE_NOVEL, "text/plain")},
        data={"title": "凡人修仙之仙界篇（节选）", "author": "忘语"},
    )
    assert up.status_code == 201, up.text
    novel = up.json()
    novel_id = novel["id"]
    assert novel["chapter_count"] == 2
    assert novel["chunk_count"] == 2

    # 4. List chapters & fetch chapter detail
    chs = client.get(f"/api/v1/novels/{novel_id}/chapters").json()
    assert chs["total"] == 2
    ch1_id = chs["items"][0]["id"]
    ch1 = client.get(f"/api/v1/chapters/{ch1_id}").json()
    assert "忘忧酒馆" in ch1["content"]
    assert len(ch1["chunks"]) == 1

    # 5. Launch FULL_PIPELINE job (runs inline in mock mode during test)
    job_res = client.post(
        f"/api/v1/novels/{novel_id}/jobs",
        json={"job_type": "FULL_PIPELINE", "budget_limit": 10.0, "run_inline": True},
    )
    assert job_res.status_code == 201, job_res.text
    job_id = job_res.json()["id"]

    job_status = client.get(f"/api/v1/jobs/{job_id}").json()
    assert job_status["state"] == "completed"
    assert job_status["spent_cost"] > 0

    # Usage stats
    usage = client.get(f"/api/v1/novels/{novel_id}/usage").json()
    assert usage["total_input_tokens"] > 0
    assert len(usage["by_stage"]) >= 2

    # 6. Search characters
    chars = client.get(f"/api/v1/novels/{novel_id}/characters", params={"q": "韩立"}).json()
    assert chars["total"] >= 1
    hanli = chars["items"][0]
    hanli_id = hanli["id"]

    detail = client.get(f"/api/v1/characters/{hanli_id}").json()
    assert detail["canonical_name"] == "韩立"

    mentions = client.get(f"/api/v1/characters/{hanli_id}/mentions").json()
    assert mentions["total"] >= 1

    events = client.get(f"/api/v1/characters/{hanli_id}/events").json()
    assert len(events) >= 1

    rels = client.get(f"/api/v1/characters/{hanli_id}/relationships").json()
    assert len(rels) >= 1
    rel_id = rels[0]["id"]

    # 7. Ego Graph & Relationship Evidence
    graph = client.get(
        "/api/v1/graph/neighbors",
        params={"center_character_id": hanli_id, "depth": 1},
    ).json()
    assert graph["center_character_id"] == hanli_id
    assert len(graph["nodes"]) >= 2
    assert len(graph["edges"]) >= 1

    rel_evs = client.get(f"/api/v1/relationships/{rel_id}/evidence").json()
    assert len(rel_evs) >= 1
    ev_detail = client.get(f"/api/v1/evidence/{rel_evs[0]['id']}").json()
    assert ev_detail["verification_status"] == "verified"
    assert ev_detail["utf16_end_offset"] > ev_detail["utf16_start_offset"]

    # 8. Deep Analysis Reports (verify unbacked 'fact' was downgraded to 'inference')
    analyses = client.get(f"/api/v1/characters/{hanli_id}/analyses").json()
    assert len(analyses) >= 1
    for claim in analyses[0]["claims"]:
        if not claim["evidence_ids"]:
            assert claim["claim_type"] != "fact"

    # 9. Identity Merge & Split (reversible audit)
    all_chars = client.get(f"/api/v1/novels/{novel_id}/characters").json()["items"]
    assert len(all_chars) >= 2
    other_id = [c["id"] for c in all_chars if c["id"] != hanli_id][0]
    merge_res = client.post(
        "/api/v1/identities/merge",
        json={
            "source_character_id": other_id,
            "target_character_id": hanli_id,
            "reason": "测试合并审计",
        },
    )
    assert merge_res.status_code == 200
    assert merge_res.json()["decision"] == "merge"

    split_res = client.post(
        "/api/v1/identities/split",
        json={
            "source_character_id": hanli_id,
            "new_canonical_name": "韩立分身",
            "mention_ids": [],
            "reason": "测试拆分审计",
        },
    )
    assert split_res.status_code == 200
    assert split_res.json()["new_character"]["canonical_name"] == "韩立分身"

    # 10. Exports (JSON, CSV, GraphML)
    for fmt in ("json", "csv", "graphml"):
        exp = client.post(f"/api/v1/novels/{novel_id}/exports", json={"format": fmt})
        assert exp.status_code == 201, exp.text
        exp_id = exp.json()["id"]
        dl = client.get(f"/api/v1/exports/{exp_id}/download")
        assert dl.status_code == 200
        assert len(dl.content) > 20
