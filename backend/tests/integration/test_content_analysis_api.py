"""内容分析 API 集成测试。

COS 是唯一存储：create_analysis 的上传循环现在把每个样本 stage 到
workspace() 再 publish 到 COS，所以任何真正触达该循环并返回 201 的用例都
需要真实 COS 凭证（`@requires_cos` + `cos_prefix`）——region_hint 校验失败
的 400 用例在到达上传循环之前就返回，不需要。
"""
import json
import pytest
from sqlalchemy import select
from app.models.project import ContentAnalysis, Project, ProjectStatus
from app.services import object_store
from tests.integration.conftest_cos import requires_cos

HEADERS = {"X-User-Name": "test-user"}


@requires_cos
async def test_create_analysis_uploads_and_enqueues(client, db_session_factory, cos_prefix):
    files = [
        ("files", ("a.mp4", b"fake-bytes-a", "video/mp4")),
        ("files", ("b.mp4", b"fake-bytes-b", "video/mp4")),
    ]
    r = await client.post("/api/analyses",
                          data={"title": "美妆A", "region_hint": "en"},
                          files=files, headers=HEADERS)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["title"] == "美妆A"
    assert len(body["samples"]) == 2
    assert body["status"] == "uploading"
    # 真入队（arq 被 fixture mock）
    client.arq.enqueue_job.assert_awaited()
    args = client.arq.enqueue_job.call_args.args
    assert args[0] == "run_content_analysis"
    # 文件真发布到 COS（不再是本地磁盘路径）
    async with db_session_factory() as s:
        row = (await s.execute(select(ContentAnalysis))).scalars().first()
        key = row.samples[0].video_path
        assert key.startswith("analyses/")
        assert await object_store.exists(key)


async def test_create_analysis_rejects_invalid_region_hint(client, db_session_factory):
    """BLOCKING 1: region_hint is passed straight into faster-whisper's strict
    language= kwarg. Free text like "en-US" must be rejected at the API
    boundary with a 400 naming the offending value — not silently accepted
    and left to crash every sample's transcription inside the worker."""
    files = [("files", ("a.mp4", b"fake-bytes-a", "video/mp4"))]
    r = await client.post("/api/analyses",
                          data={"title": "美妆A", "region_hint": "en-US"},
                          files=files, headers=HEADERS)
    assert r.status_code == 400, r.text
    assert "en-US" in r.json()["detail"]


async def test_create_analysis_rejects_freetext_region_hint(client, db_session_factory):
    files = [("files", ("a.mp4", b"fake-bytes-a", "video/mp4"))]
    r = await client.post("/api/analyses",
                          data={"title": "美妆A", "region_hint": "美国"},
                          files=files, headers=HEADERS)
    assert r.status_code == 400, r.text
    assert "美国" in r.json()["detail"]


@requires_cos
async def test_create_analysis_accepts_valid_asr_region_hint(client, db_session_factory, cos_prefix):
    files = [("files", ("a.mp4", b"fake-bytes-a", "video/mp4"))]
    r = await client.post("/api/analyses",
                          data={"title": "美妆A", "region_hint": "en"},
                          files=files, headers=HEADERS)
    assert r.status_code == 201, r.text
    assert r.json()["region_hint"] == "en"


@requires_cos
async def test_create_analysis_accepts_yue_region_hint(client, db_session_factory, cos_prefix):
    """yue (Cantonese) is supported by faster-whisper's large-v3 model and is
    plausible reference material for this tool, but was wrongly rejected by
    the old hand-rolled ISO-639-1 code set (which doesn't include it)."""
    files = [("files", ("a.mp4", b"fake-bytes-a", "video/mp4"))]
    r = await client.post("/api/analyses",
                          data={"title": "粤语账号", "region_hint": "yue"},
                          files=files, headers=HEADERS)
    assert r.status_code == 201, r.text
    assert r.json()["region_hint"] == "yue"


async def test_create_analysis_rejects_jv_region_hint(client, db_session_factory):
    """jv is a valid ISO-639-1 code (Javanese) and used to pass the old
    hand-rolled validation, but faster-whisper spells Javanese "jw" — passing
    "jv" through to it raises ValueError on every sample, so it must be
    rejected here instead."""
    files = [("files", ("a.mp4", b"fake-bytes-a", "video/mp4"))]
    r = await client.post("/api/analyses",
                          data={"title": "美妆A", "region_hint": "jv"},
                          files=files, headers=HEADERS)
    assert r.status_code == 400, r.text
    assert "jv" in r.json()["detail"]


@requires_cos
async def test_create_analysis_accepts_absent_region_hint(client, db_session_factory, cos_prefix):
    files = [("files", ("a.mp4", b"fake-bytes-a", "video/mp4"))]
    r = await client.post("/api/analyses",
                          data={"title": "美妆B"},
                          files=files, headers=HEADERS)
    assert r.status_code == 201, r.text
    assert r.json()["region_hint"] is None


async def test_get_and_list_analysis(client, db_session_factory):
    async with db_session_factory() as s:
        a = ContentAnalysis(title="t"); s.add(a); await s.commit(); aid = a.id
    r = await client.get(f"/api/analyses/{aid}", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["id"] == aid
    r2 = await client.get("/api/analyses", headers=HEADERS)
    assert r2.status_code == 200
    assert r2.json()["total"] >= 1


async def test_attach_brief_snapshots_into_project(client, db_session_factory):
    async with db_session_factory() as s:
        a = ContentAnalysis(title="t", status="completed",
                            brief_json='{"screenwriter_directives":"开场抛悬念"}')
        p = Project(title="p", theme_text="th", creator_name="test-user",
                    status=ProjectStatus.DRAFT.value)
        s.add_all([a, p]); await s.commit(); aid, pid = a.id, p.id
    r = await client.post(f"/api/projects/{pid}/attach-brief",
                          json={"analysis_id": aid}, headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    # C3: attach-brief's ProjectResponse must report the attachment it just made.
    assert body["content_analysis_id"] == aid
    assert json.loads(body["attached_brief_json"])["screenwriter_directives"] == "开场抛悬念"
    async with db_session_factory() as s:
        p = (await s.execute(select(Project).where(Project.id == pid))).scalar_one()
        assert p.content_analysis_id == aid
        assert json.loads(p.attached_brief_json)["screenwriter_directives"] == "开场抛悬念"


async def test_attach_brief_requires_auth(client, db_session_factory):
    """BLOCKING 4: attach-brief is a mutating endpoint — every other one in
    this codebase requires X-User-Name (see pipeline.py/voice.py/
    image_candidates.py/projects.py and create_analysis in this module)."""
    async with db_session_factory() as s:
        a = ContentAnalysis(title="t", status="completed",
                            brief_json='{"screenwriter_directives":"x"}')
        p = Project(title="p", theme_text="th", creator_name="test-user",
                    status=ProjectStatus.DRAFT.value)
        s.add_all([a, p]); await s.commit(); aid, pid = a.id, p.id
    r = await client.post(f"/api/projects/{pid}/attach-brief", json={"analysis_id": aid})
    assert r.status_code == 400
    async with db_session_factory() as s:
        p = (await s.execute(select(Project).where(Project.id == pid))).scalar_one()
        assert p.content_analysis_id is None


async def test_stream_analysis_unknown_id_404s(client):
    """C2: unlike the old behaviour (silent open stream forever), an unknown
    analysis id must 404, mirroring app/api/stream.py's stream_events."""
    r = await client.get("/api/analyses/does-not-exist/stream", headers=HEADERS)
    assert r.status_code == 404


async def test_stream_analysis_sends_state_snapshot(client, db_session_factory, redis):
    """C2: a client connecting after the worker already finished must still
    learn the terminal state via an initial state_snapshot event, instead of
    a permanently silent open stream. `client` fixture requested first so
    app.main (and its routers) are fully imported — see precedent in
    test_stream_snapshot_candidates.py."""
    from app.api.content_analysis import _content_analysis_stream_generator

    async with db_session_factory() as s:
        a = ContentAnalysis(title="t", status="analyzing", region_hint="en")
        s.add(a)
        await s.commit()
        aid = a.id

    gen = _content_analysis_stream_generator(redis, aid)
    first_event_json = await gen.__anext__()
    await gen.aclose()

    event = json.loads(first_event_json)
    assert event["type"] == "state_snapshot"
    assert event["data"]["id"] == aid
    assert event["data"]["status"] == "analyzing"
