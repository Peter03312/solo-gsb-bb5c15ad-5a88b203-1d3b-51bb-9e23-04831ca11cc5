"""HTTP API 集成测试：登记、补传幂等/冲突、裁定、审核重算、发布冻结、错误处理。

每个测试使用独立临时数据库，互不干扰。
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.config import get_settings
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    # get_settings 在首测时可能已缓存；这里显式覆盖其缓存
    get_settings.cache_clear()
    monkeypatch.setenv("FLOOD_DB_PATH", db_path)
    monkeypatch.setenv("FLOOD_SEED_DEMO", "false")
    monkeypatch.setenv("FLOOD_REVIEW_TOKEN", "secret")
    get_settings.cache_clear()
    with TestClient(app) as c:
        yield c


H = {"X-Reviewer-Token": "secret", "X-Reviewer": "tester"}


def iso(minutes_from_now: int) -> str:
    t = datetime.now(timezone(timedelta(hours=8))) + timedelta(minutes=minutes_from_now)
    return t.replace(microsecond=0).isoformat()


def register_station(client, code="ST1", device="D1"):
    r = client.post(
        "/api/stations",
        headers=H,
        json={
            "code": code,
            "name": "一号站",
            "rise_threshold": "5.00",
            "clear_threshold": "4.00",
            "devices": [{"device_no": device, "name": "水位计1"}],
        },
    )
    assert r.status_code == 201, r.text


def ingest(client, seq, minutes, level, device="D1"):
    r = client.post(
        "/api/readings",
        json={"device_no": device, "seq": seq, "observed_at": iso(minutes), "water_level": level},
    )
    assert r.status_code == 201, (seq, r.text)
    return r.json()


def test_health_and_register(client):
    assert client.get("/health").json()["status"] == "ok"
    register_station(client)
    detail = client.get("/api/stations/ST1").json()
    assert detail["rise_threshold"] == "5.00"
    assert client.get("/api/devices").json()["devices"][0]["device_no"] == "D1"


def test_register_invalid_threshold(client):
    r = client.post(
        "/api/stations",
        headers=H,
        json={"code": "ST-BAD", "name": "x", "rise_threshold": "4", "clear_threshold": "5"},
    )
    assert r.status_code == 422


def test_unknown_device_rejected(client):
    register_station(client)
    r = client.post(
        "/api/readings",
        json={"device_no": "NOPE", "seq": 1, "observed_at": iso(-30), "water_level": "1.0"},
    )
    assert r.status_code == 404


def test_naive_time_rejected(client):
    register_station(client)
    r = client.post(
        "/api/readings",
        json={"device_no": "D1", "seq": 1, "observed_at": "2026-09-01T10:00:00", "water_level": "1.0"},
    )
    assert r.status_code == 422


def test_future_time_rejected(client):
    register_station(client)
    r = client.post(
        "/api/readings",
        json={"device_no": "D1", "seq": 1, "observed_at": iso(60 * 24), "water_level": "1.0"},
    )
    assert r.status_code == 422


def test_ingest_idempotent_and_conflict(client):
    register_station(client)
    ts = iso(-60)
    body = {"device_no": "D1", "seq": 1, "observed_at": ts, "water_level": "1.20"}
    r1 = client.post("/api/readings", json=body)
    assert r1.status_code == 201
    assert r1.json()["replayed"] is False

    # 同内容重试（时间换等价的 UTC 写法，存储 epoch 相同）
    body_re = dict(body)
    body_re["observed_at"] = datetime.fromisoformat(ts).astimezone(timezone.utc).isoformat()
    r2 = client.post("/api/readings", json=body_re)
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert r2.json()["id"] == r1.json()["id"]

    # 异内容冲突
    r3 = client.post("/api/readings", json=dict(body, water_level="9.99"))
    assert r3.status_code == 409
    # 同序号不同时间同样冲突
    r4 = client.post("/api/readings", json=dict(body, observed_at=iso(-59)))
    assert r4.status_code == 409


def test_warning_and_clear_timeline_then_publish_freeze(client):
    register_station(client)
    # seq1 为较早的一个低水位读数；seq2..8 构成：低-低，高x3(跨度10min)，低x2(跨度5min)
    ingest(client, 1, -55, "3.2")
    minutes_levels = [(-50, "3.5"), (-45, "3.8"), (-40, "5.4"), (-35, "5.8"),
                      (-30, "6.2"), (-25, "3.9"), (-20, "3.7")]
    for seq, (m, level) in enumerate(minutes_levels, start=2):
        ingest(client, seq, m, level)

    r = client.get("/api/stations/ST1/timeline")
    tl = r.json()
    assert [e["event"] for e in tl["events"]] == ["rised", "cleared"]
    assert tl["state"] == "normal"
    assert tl["in_sync_with_public"] is False
    assert len(tl["segments"]) == 1

    # 发布冻结
    r = client.post("/api/publish", headers=H, json={"stations": ["ST1"]})
    assert r.status_code == 201, r.text
    v1 = r.json()["published"][0]["version"]
    pub = client.get("/public/stations/ST1/timeline").json()
    assert pub["version"] == v1
    assert [e["event"] for e in pub["events"]] == ["rised", "cleared"]
    assert len(pub["readings"]) == 8

    # 补传一段会重新触发预警的读数组（接续上一读数间隔 5 分钟，同段 3 个高位、跨度 10 分钟）
    for seq, (m, level) in [(100, (-15, "8.8")), (101, (-10, "8.9")), (102, (-5, "9.0"))]:
        ingest(client, seq, m, level)
    draft = client.get("/api/stations/ST1/timeline").json()
    assert draft["in_sync_with_public"] is False
    assert [e["event"] for e in draft["events"]] == ["rised", "cleared", "rised"]
    pub_before = client.get("/public/stations/ST1/timeline").json()
    assert pub_before["version"] == v1
    assert len(pub_before["readings"]) == 8
    assert pub_before["state"] == "normal"

    # 重新发布后公开结果才更新
    r = client.post("/api/publish", headers=H, json={"stations": ["ST1"]})
    v2 = r.json()["published"][0]["version"]
    assert v2 == v1 + 1
    pub_after = client.get("/public/stations/ST1/timeline").json()
    assert pub_after["version"] == v2
    assert len(pub_after["readings"]) == 11
    assert pub_after["state"] == "warning"

    # 版本历史可读，旧版本仍可取
    versions = client.get("/api/stations/ST1/publications").json()["publications"]
    assert [p["version"] for p in versions] == [1, 2]
    old = client.get("/public/stations/ST1/timeline", params={"version": 1}).json()
    assert old["state"] == "normal"


def test_correction_recomputes_and_keeps_history(client):
    register_station(client)
    ingest(client, 1, -15, "6.0")
    ingest(client, 2, -10, "6.1")
    rid3 = ingest(client, 3, -5, "6.2")["id"]  # 跨度 10 分钟 -> 预警
    tl = client.get("/api/stations/ST1/timeline").json()
    assert [e["event"] for e in tl["events"]] == ["rised"]

    # 缺原因 / 无令牌 / 同值 均报错
    assert client.post(f"/api/readings/{rid3}/correct", headers=H, json={"water_level": "6.2"}).status_code == 422
    assert client.post(f"/api/readings/{rid3}/correct", json={"water_level": "3.0", "reason": "x"}).status_code == 401
    r = client.post(
        f"/api/readings/{rid3}/correct", headers=H,
        json={"water_level": "3.0", "reason": "现场校准修正"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["water_level"] == "3.0"

    revs = client.get(f"/api/readings/{rid3}/revisions").json()["revisions"]
    assert len(revs) == 1
    assert revs[0]["old_water_level"] == "6.2"
    assert revs[0]["new_water_level"] == "3.0"
    assert revs[0]["action"] == "correct"
    assert revs[0]["reviewer"] == "tester"

    # 裁定随之重算：高位连续性被打断，无预警
    tl = client.get("/api/stations/ST1/timeline").json()
    assert tl["events"] == []

    # 再次更正保留历次修订
    r = client.post(
        f"/api/readings/{rid3}/correct", headers=H,
        json={"water_level": "6.3", "reason": "二次复核"},
    )
    assert r.status_code == 200
    revs = client.get(f"/api/readings/{rid3}/revisions").json()["revisions"]
    assert [v["new_water_level"] for v in revs] == ["3.0", "6.3"]
    tl = client.get("/api/stations/ST1/timeline").json()
    assert [e["event"] for e in tl["events"]] == ["rised"]


def test_void_reading(client):
    register_station(client)
    ingest(client, 1, -15, "6.0")
    rid2 = ingest(client, 2, -10, "6.1")["id"]
    rid3 = ingest(client, 3, -5, "6.2")["id"]
    assert [e["event"] for e in client.get("/api/stations/ST1/timeline").json()["events"]] == ["rised"]

    r = client.post(f"/api/readings/{rid3}/void", headers=H, json={"reason": "传感器故障"})
    assert r.status_code == 200
    assert r.json()["status"] == "voided"
    # 重复作废、作废后再更正都报错
    assert client.post(f"/api/readings/{rid3}/void", headers=H, json={"reason": "再试"}).status_code == 422
    assert client.post(f"/api/readings/{rid3}/correct", headers=H,
                       json={"water_level": "1", "reason": "x"}).status_code == 422

    tl = client.get("/api/stations/ST1/timeline").json()
    assert len(tl["readings"]) == 2
    assert tl["events"] == []

    # 作废历史可查；默认列表只含有效读数，可按 status 查全部
    revs = client.get(f"/api/readings/{rid3}/revisions").json()["revisions"]
    assert revs[0]["action"] == "void" and revs[0]["old_water_level"] == "6.2"
    actives = client.get("/api/stations/ST1/readings").json()["readings"]
    assert {r["status"] for r in actives} == {"active"}
    with_void = client.get("/api/stations/ST1/readings", params={"status": "all"}).json()["readings"]
    assert len(with_void) == 3 and rid3 in [r["id"] for r in with_void]


def test_segment_break_blocks_rise_but_not_auto_clear(client):
    register_station(client)
    ingest(client, 1, -40, "6.0")
    ingest(client, 2, -35, "6.0")
    # 间隔 20 分钟断段，仅一个高位读数 -> 不预警
    ingest(client, 3, -15, "6.0")
    tl = client.get("/api/stations/ST1/timeline").json()
    assert tl["events"] == []
    assert len(tl["segments"]) == 2


def test_bad_water_level(client):
    register_station(client)
    assert client.post("/api/readings", json={
        "device_no": "D1", "seq": 200, "observed_at": iso(-5), "water_level": "abc"}).status_code == 422
    assert client.post("/api/readings", json={
        "device_no": "D1", "seq": -1, "observed_at": iso(-5), "water_level": 1}).status_code == 422
    assert client.post("/api/readings", json={
        "device_no": "D1", "seq": 201, "observed_at": iso(-5), "water_level": -3}).status_code == 422


def test_publish_all_stations_and_public_index(client):
    register_station(client, code="ST1", device="D1")
    register_station(client, code="ST2", device="D2")
    r = client.post("/api/publish", headers=H, json={})
    assert r.status_code == 201
    assert {p["station_code"] for p in r.json()["published"]} == {"ST1", "ST2"}
    index = client.get("/public/stations").json()["stations"]
    assert {s["code"] for s in index} == {"ST1", "ST2"}

    # 从未发布的站没有公开时间线
    register_station(client, code="ST3", device="D3")
    assert client.get("/public/stations/ST3/timeline").status_code == 404
