"""端到端 API 测试：裁定规则、幂等补传、审核修订、发布冻结、并发安全。"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

BASE = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat()


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        min_water_level=-50,
        max_water_level=1000,
        segment_gap_minutes=15,
        rise_min_count=3,
        rise_min_span_minutes=10,
        clear_min_count=2,
        clear_min_span_minutes=5,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def station(client):
    r = client.post("/stations", json={"name": "青石峡站", "rise_threshold": 10.0, "clear_threshold": 8.0})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture()
def device(client, station):
    r = client.post(f"/stations/{station}/devices", json={"device_no": "DEV-001"})
    assert r.status_code == 201, r.text
    return "DEV-001"


def upload(client, device_no, seq, minutes, level):
    return client.post(
        "/readings",
        json={
            "device_no": device_no,
            "seq": seq,
            "observed_at": iso(BASE + timedelta(minutes=minutes)),
            "water_level": level,
        },
    )


def timeline(client, station_id):
    r = client.get(f"/stations/{station_id}/timeline")
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- 登记与校验

def test_create_station_and_device(client, station, device):
    st = client.get(f"/stations/{station}").json()
    assert st["name"] == "青石峡站"
    assert st["rise_threshold"] == 10.0
    assert st["clear_threshold"] == 8.0
    assert st["devices"][0]["device_no"] == "DEV-001"


def test_invalid_thresholds_rejected(client):
    r = client.post("/stations", json={"name": "x", "rise_threshold": 8.0, "clear_threshold": 10.0})
    assert r.status_code == 400
    r = client.post("/stations", json={"name": "x", "rise_threshold": 10.0, "clear_threshold": 10.0})
    assert r.status_code == 400
    r = client.post("/stations", json={"name": "x", "rise_threshold": "abc", "clear_threshold": 1.0})
    assert r.status_code == 400


def test_duplicate_device_conflict(client, station, device):
    r = client.post(f"/stations/{station}/devices", json={"device_no": "DEV-001"})
    assert r.status_code == 409


def test_device_on_unknown_station(client):
    r = client.post("/stations/999/devices", json={"device_no": "DEV-X"})
    assert r.status_code == 404


# ---------------------------------------------------------------- 补传校验与幂等

def test_unknown_device_rejected(client):
    r = upload(client, "NOPE", 1, 0, 5.0)
    assert r.status_code == 404


def test_naive_time_rejected(client, device):
    r = client.post(
        "/readings",
        json={"device_no": device, "seq": 1, "observed_at": "2026-10-01T00:00:00", "water_level": 5.0},
    )
    assert r.status_code == 400


def test_bad_time_string_rejected(client, device):
    r = client.post(
        "/readings",
        json={"device_no": device, "seq": 1, "observed_at": "not-a-time", "water_level": 5.0},
    )
    assert r.status_code == 400


def test_invalid_water_level_rejected(client, device):
    for bad in ("abc", 100000.0, -9999.0):
        r = client.post(
            "/readings",
            json={"device_no": device, "seq": 1, "observed_at": iso(BASE), "water_level": bad},
        )
        assert r.status_code == 400, f"water_level={bad} 应被拒绝"
    # NaN / Infinity 不是合法 JSON 数值，以原始报文发送也应被拒绝
    for bad in ("NaN", "Infinity", "-Infinity"):
        r = client.post(
            "/readings",
            content=(
                '{"device_no": "%s", "seq": 1, "observed_at": "%s", "water_level": %s}'
                % (device, iso(BASE), bad)
            ),
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 400, f"water_level={bad} 应被拒绝"


def test_idempotent_retry_same_content(client, device):
    r1 = upload(client, device, 1, 0, 5.0)
    assert r1.status_code == 201
    assert r1.json()["deduplicated"] is False
    # 同内容重试（甚至换时区表示同一时刻）→ 返回原结果
    r2 = client.post(
        "/readings",
        json={
            "device_no": device,
            "seq": 1,
            "observed_at": (BASE).astimezone(timezone(timedelta(hours=8))).isoformat(),
            "water_level": 5.0,
        },
    )
    assert r2.status_code == 200
    assert r2.json()["deduplicated"] is True
    assert r2.json()["id"] == r1.json()["id"]
    rows = client.get(f"/stations/1/readings").json()
    assert len(rows) == 1


def test_conflicting_seq_rejected(client, device):
    r1 = upload(client, device, 1, 0, 5.0)
    assert r1.status_code == 201
    r2 = upload(client, device, 1, 0, 6.0)  # 同号同序号异内容
    assert r2.status_code == 409
    r3 = upload(client, device, 1, 5, 5.0)
    assert r3.status_code == 409
    rows = client.get(f"/stations/1/readings").json()
    assert len(rows) == 1  # 冲突不产生新记录


# ---------------------------------------------------------------- 裁定规则

def test_warning_requires_3_readings_and_10min_span(client, station, device):
    upload(client, device, 1, 0, 11.0)
    upload(client, device, 2, 4, 11.0)
    upload(client, device, 3, 8, 11.0)  # 3 个但首尾仅 8 分钟
    tl = timeline(client, station)
    assert tl["status"] == "normal"
    assert tl["events"] == []
    upload(client, device, 4, 12, 11.0)  # 4 个，首尾 12 分钟 → 在此读数预警
    tl = timeline(client, station)
    assert tl["status"] == "warning"
    assert len(tl["events"]) == 1
    ev = tl["events"][0]
    assert ev["type"] == "warning"
    assert ev["at"] == iso(BASE + timedelta(minutes=12)).replace("+00:00", "Z")
    assert ev["run_count"] == 4


def test_out_of_order_uploads_reordered(client, station, device):
    # 乱序补传，按观测时间重排后仍应触发预警
    upload(client, device, 3, 10, 12.0)
    upload(client, device, 1, 0, 12.0)
    upload(client, device, 2, 5, 12.0)
    tl = timeline(client, station)
    assert tl["status"] == "warning"
    assert tl["events"][0]["at"] == iso(BASE + timedelta(minutes=10)).replace("+00:00", "Z")


def test_segment_break_resets_rise_run(client, station, device):
    upload(client, device, 1, 0, 11.0)
    upload(client, device, 2, 6, 11.0)
    upload(client, device, 3, 30, 11.0)  # 间隔 24 分钟 > 15 → 断段，重新计数
    tl = timeline(client, station)
    assert tl["status"] == "normal"
    assert len(tl["segments"]) == 2
    upload(client, device, 4, 35, 11.0)
    upload(client, device, 5, 40, 11.0)  # 新段内 3 个、首尾 10 分钟 → 预警
    tl = timeline(client, station)
    assert tl["status"] == "warning"
    assert tl["events"][0]["at"] == iso(BASE + timedelta(minutes=40)).replace("+00:00", "Z")


def test_interruption_by_low_reading_resets_run(client, station, device):
    upload(client, device, 1, 0, 11.0)
    upload(client, device, 2, 5, 11.0)
    upload(client, device, 3, 10, 9.0)   # 未达上升阈值，连续被打断
    upload(client, device, 4, 15, 11.0)
    upload(client, device, 5, 20, 11.0)
    tl = timeline(client, station)
    assert tl["status"] == "normal"


def test_clear_requires_2_readings_and_5min_span(client, station, device):
    for i, m in enumerate([0, 5, 10], start=1):
        upload(client, device, i, m, 12.0)
    assert timeline(client, station)["status"] == "warning"
    upload(client, device, 4, 12, 7.0)
    upload(client, device, 5, 14, 7.0)  # 2 个但首尾仅 2 分钟
    assert timeline(client, station)["status"] == "warning"
    upload(client, device, 6, 18, 7.0)  # 3 个，首尾 6 分钟 → 解除
    tl = timeline(client, station)
    assert tl["status"] == "normal"
    assert tl["events"][-1]["type"] == "cleared"
    assert tl["events"][-1]["at"] == iso(BASE + timedelta(minutes=18)).replace("+00:00", "Z")


def test_segment_break_does_not_auto_clear(client, station, device):
    for i, m in enumerate([0, 5, 10], start=1):
        upload(client, device, i, m, 12.0)
    assert timeline(client, station)["status"] == "warning"
    upload(client, device, 4, 12, 7.0)   # 1 个低水位
    upload(client, device, 5, 40, 7.0)   # 断段（28 分钟）不自动解除，但解除计数跨段延续
    tl = timeline(client, station)
    assert tl["status"] == "normal"      # 2 个低水位、首尾 28 分钟 ≥ 5 → 解除
    assert tl["events"][-1]["type"] == "cleared"


def test_warning_can_retrigger_after_clear(client, station, device):
    for i, m in enumerate([0, 5, 10], start=1):
        upload(client, device, i, m, 12.0)
    upload(client, device, 4, 15, 7.0)
    upload(client, device, 5, 21, 7.0)   # 解除
    upload(client, device, 6, 25, 12.0)
    upload(client, device, 7, 30, 12.0)
    upload(client, device, 8, 40, 12.0)  # 再次预警
    tl = timeline(client, station)
    types = [e["type"] for e in tl["events"]]
    assert types == ["warning", "cleared", "warning"]
    assert tl["status"] == "warning"


# ---------------------------------------------------------------- 审核修订

def _make_warning(client, device):
    for i, m in enumerate([0, 5, 10], start=1):
        upload(client, device, i, m, 12.0)


def test_void_reading_recalculates(client, station, device):
    _make_warning(client, device)
    assert timeline(client, station)["status"] == "warning"
    r = client.post("/readings/2/void", json={"reason": "传感器故障", "auditor": "王审"})
    assert r.status_code == 200
    assert r.json()["reading"]["status"] == "voided"
    assert r.json()["pending_adjudication"]["status"] == "normal"  # 裁定随之重算
    # 默认列表不含已作废，include_voided 可见
    assert len(client.get(f"/stations/{station}/readings").json()) == 2
    assert len(client.get(f"/stations/{station}/readings?include_voided=true").json()) == 3
    # 原值与修订历史可查
    hist = client.get("/readings/2/revisions").json()
    assert hist["original"]["water_level"] == 12.0
    assert len(hist["revisions"]) == 1
    assert hist["revisions"][0]["action"] == "void"
    assert hist["revisions"][0]["reason"] == "传感器故障"
    assert hist["revisions"][0]["old_water_level"] == 12.0


def test_void_requires_reason(client, device):
    _make_warning(client, device)
    r = client.post("/readings/1/void", json={"reason": ""})
    assert r.status_code == 400
    r = client.post("/readings/1/void", json={})
    assert r.status_code == 400


def test_double_void_conflict(client, device):
    _make_warning(client, device)
    client.post("/readings/1/void", json={"reason": "r"})
    r = client.post("/readings/1/void", json={"reason": "again"})
    assert r.status_code == 409


def test_correct_reading_recalculates(client, station, device):
    _make_warning(client, device)
    assert timeline(client, station)["status"] == "warning"
    # 把第 2 条更正为低于阈值 → 连续被打断，预警消失
    r = client.post("/readings/2/correct", json={"water_level": 5.0, "reason": "抄录错误", "auditor": "李审"})
    assert r.status_code == 200
    assert r.json()["reading"]["water_level"] == 5.0
    assert r.json()["reading"]["original_water_level"] == 12.0
    assert r.json()["pending_adjudication"]["status"] == "normal"
    hist = client.get("/readings/2/revisions").json()
    assert hist["original"]["water_level"] == 12.0
    assert hist["revisions"][0]["action"] == "correct"
    assert hist["revisions"][0]["new_water_level"] == 5.0


def test_correct_time_reorders(client, station, device):
    upload(client, device, 1, 0, 12.0)
    upload(client, device, 2, 5, 12.0)
    upload(client, device, 3, 100, 12.0)  # 间隔过大，断段 → 无预警
    assert timeline(client, station)["status"] == "normal"
    # 更正观测时间使其落入同段 → 触发预警
    r = client.post("/readings/3/correct", json={"observed_at": iso(BASE + timedelta(minutes=10)), "reason": "时钟偏移"})
    assert r.status_code == 200
    assert r.json()["pending_adjudication"]["status"] == "warning"


def test_correct_voided_reading_conflict(client, device):
    _make_warning(client, device)
    client.post("/readings/1/void", json={"reason": "r"})
    r = client.post("/readings/1/correct", json={"water_level": 1.0, "reason": "r"})
    assert r.status_code == 409


def test_revision_history_accumulates(client, device):
    _make_warning(client, device)
    client.post("/readings/1/correct", json={"water_level": 11.5, "reason": "第一次更正"})
    client.post("/readings/1/correct", json={"water_level": 11.8, "reason": "第二次更正", "auditor": "赵审"})
    hist = client.get("/readings/1/revisions").json()
    assert hist["original"]["water_level"] == 12.0
    assert [r["revision_no"] for r in hist["revisions"]] == [1, 2]
    assert hist["revisions"][0]["new_water_level"] == 11.5
    assert hist["revisions"][1]["old_water_level"] == 11.5
    assert hist["revisions"][1]["new_water_level"] == 11.8
    assert hist["reading"]["water_level"] == 11.8
    assert hist["reading"]["version"] == 3


# ---------------------------------------------------------------- 发布冻结

def test_publish_freezes_timeline(client, station, device):
    _make_warning(client, device)
    r = client.post("/publish", json={"note": "首次发布"})
    assert r.status_code == 201
    pub_id = r.json()["publication_id"]
    assert r.json()["stations"][0]["status"] == "warning"

    pub = client.get("/public/current").json()
    assert pub["publication_id"] == pub_id
    assert pub["timelines"][0]["status"] == "warning"

    # 发布后补传解除读数：待发布变化，公开结果保持冻结
    upload(client, device, 4, 15, 7.0)
    upload(client, device, 5, 21, 7.0)
    assert timeline(client, station)["status"] == "normal"
    assert client.get("/public/current").json()["timelines"][0]["status"] == "warning"
    assert client.get(f"/public/stations/{station}/timeline").json()["status"] == "warning"

    # 发布后修订同样只影响待发布
    client.post("/readings/5/void", json={"reason": "误报"})
    assert timeline(client, station)["status"] == "warning"
    assert client.get("/public/current").json()["timelines"][0]["status"] == "warning"

    # 重新发布才更新公开结果
    r2 = client.post("/publish")
    assert r2.status_code == 201
    assert client.get("/public/current").json()["timelines"][0]["status"] == "warning"
    # 撤销作废的读数无法恢复，重新补一条解除读数再发布
    upload(client, device, 6, 22, 7.0)
    client.post("/publish")
    assert client.get("/public/current").json()["timelines"][0]["status"] == "normal"
    # 历史发布版本仍可查询
    old = client.get(f"/publications/{pub_id}").json()
    assert old["timelines"][0]["status"] == "warning"


def test_public_current_404_before_any_publish(client, station):
    assert client.get("/public/current").status_code == 404
    assert client.get(f"/public/stations/{station}/timeline").status_code == 404


# ---------------------------------------------------------------- 并发安全

def test_concurrent_distinct_uploads_nothing_lost(client, device):
    errors = []

    def worker(base_seq):
        try:
            for i in range(20):
                r = upload(client, device, base_seq + i, i, 5.0 + i * 0.01)
                assert r.status_code == 201, r.text
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n * 1000,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    rows = client.get("/stations/1/readings").json()
    assert len(rows) == 160
    assert len({r["id"] for r in rows}) == 160


def test_concurrent_same_seq_same_content(client, device):
    results = []

    def worker():
        r = upload(client, device, 7, 0, 5.0)
        results.append((r.status_code, r.json()["id"]))

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    codes = sorted(c for c, _ in results)
    ids = {i for _, i in results}
    assert codes.count(201) == 1
    assert codes.count(200) == 9
    assert len(ids) == 1
    assert len(client.get("/stations/1/readings").json()) == 1


def test_concurrent_same_seq_different_content(client, device):
    outcomes = []

    def worker(level):
        r = upload(client, device, 9, 0, level)
        outcomes.append(r.status_code)

    threads = [threading.Thread(target=worker, args=(float(n),)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count(201) == 1
    assert outcomes.count(409) == 5
    assert len(client.get("/stations/1/readings").json()) == 1


def test_concurrent_publish_and_upload_consistent(client, station, device):
    _make_warning(client, device)
    errors = []

    def publisher():
        try:
            for _ in range(5):
                r = client.post("/publish")
                assert r.status_code == 201, r.text
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def uploader():
        try:
            for i in range(10):
                upload(client, device, 100 + i, 20 + i, 7.0)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=publisher), threading.Thread(target=uploader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    pubs = client.get("/publications").json()
    assert len(pubs) == 5
    assert len({p["publication_id"] for p in pubs}) == 5
    rows = client.get("/stations/1/readings").json()
    assert len(rows) == 13  # 3 条预警读数 + 10 条并发补传，一条不丢
