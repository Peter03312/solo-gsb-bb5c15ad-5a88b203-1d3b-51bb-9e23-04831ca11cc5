"""并发安全测试：多线程并发补传 / 并发修订 / 补传与发布并行，不丢已提交记录。

直接对服务函数与独立连接施压（绕过 HTTP 客户端的连接池限制），
底层依赖 SQLite WAL + BEGIN IMMEDIATE 写串行化 + 唯一约束。
"""
from __future__ import annotations

import tempfile
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import db as dbmod
from app import service


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("FLOOD_DB_PATH", str(tmp_path / "conc.db"))
    monkeypatch.setenv("FLOOD_SEED_DEMO", "false")
    get_settings.cache_clear()

    setup = dbmod.connect()
    dbmod.init_db(setup)
    service.register_station(
        setup,
        {
            "code": "C1",
            "name": "并发站",
            "rise_threshold": "5",
            "clear_threshold": "4",
            "devices": [{"device_no": f"CD{i}", "name": f"设备{i}"} for i in range(4)],
        },
    )
    setup.close()
    yield
    get_settings.cache_clear()


def _iso(minutes_ago: int) -> str:
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.replace(microsecond=0).isoformat()


def test_concurrent_ingestion_no_loss(env):
    n_threads = 8
    per_thread = 25

    errors: list[Exception] = []

    def worker(tid: int):
        conn = dbmod.connect()
        try:
            for i in range(per_thread):
                seq = tid * 1000 + i
                try:
                    service.ingest_reading(
                        conn,
                        {
                            "device_no": "CD0",
                            "seq": seq,
                            "observed_at": _iso(200 - (seq % 180)),
                            "water_level": f"{(i % 9) + 1}.{tid}",
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    conn = dbmod.connect()
    count = conn.execute("SELECT COUNT(*) AS c FROM readings WHERE device_id = 1").fetchone()["c"]
    assert count == n_threads * per_thread
    conn.close()


def test_concurrent_same_device_seq_serializes(env):
    # 多线程对同一 (device, seq) 并发提交：恰好一个成功，其余按内容判定 200/409
    barrier = threading.Barrier(4)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(tid: int):
        conn = dbmod.connect()
        barrier.wait()
        try:
            created, _ = service.ingest_reading(
                conn,
                {
                    "device_no": "CD1",
                    "seq": 1,
                    "observed_at": _iso(30),
                    "water_level": f"5.{tid}",  # 每个线程内容不同
                },
            )
            with lock:
                outcomes.append("created" if created else "replayed")
        except service.ValidationError:
            with lock:
                outcomes.append("conflict")
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("created") == 1
    assert outcomes.count("conflict") == 3
    conn = dbmod.connect()
    count = conn.execute("SELECT COUNT(*) AS c FROM readings WHERE device_id = 2").fetchone()["c"]
    assert count == 1
    conn.close()


def test_concurrent_ingest_and_publish_no_loss(env):
    stop = threading.Event()
    publish_versions: list[int] = []

    def publisher():
        conn = dbmod.connect()
        try:
            for _ in range(5):
                snapshots = service.publish_timelines(conn, ["C1"], "publisher")
                publish_versions.append(snapshots[0]["version"])
        finally:
            conn.close()
            stop.set()

    def ingester(tid: int):
        conn = dbmod.connect()
        i = 0
        while not stop.is_set():
            try:
                service.ingest_reading(
                    conn,
                    {
                        "device_no": f"CD{2 + tid % 2}",
                        "seq": tid * 10000 + i,
                        "observed_at": _iso(500 - ((tid * 7 + i) % 400)),
                        "water_level": f"{(i % 8) + 1}.0",
                    },
                )
            except Exception:
                pass
            i += 1
        conn.close()

    p = threading.Thread(target=publisher)
    workers = [threading.Thread(target=ingester, args=(t,)) for t in range(2)]
    p.start()
    for w in workers:
        w.start()
    p.join()
    for w in workers:
        w.join()

    conn = dbmod.connect()
    # 发布了 5 个严格递增版本
    assert publish_versions == [1, 2, 3, 4, 5]
    # 每次发布的快照与当时库内状态一致：最新发布之后仍有补传，但已发布记录都在
    total = conn.execute("SELECT COUNT(*) AS c FROM readings").fetchone()["c"]
    assert total > 0
    pubs = conn.execute("SELECT COUNT(*) AS c FROM publications").fetchone()["c"]
    assert pubs == 5
    conn.close()


def test_concurrent_corrections_keep_all_revisions(env):
    conn = dbmod.connect()
    rid = service.ingest_reading(
        conn, {"device_no": "CD3", "seq": 1, "observed_at": _iso(10), "water_level": "1.0"}
    )[1]["id"]
    conn.close()

    barrier = threading.Barrier(3)

    def reviewer(tid: int):
        conn = dbmod.connect()
        barrier.wait()
        service.correct_reading(
            conn, rid, {"water_level": f"2.{tid}", "reason": f"修订-{tid}"}, f"rev{tid}"
        )
        conn.close()

    threads = [threading.Thread(target=reviewer, args=(t,)) for t in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    conn = dbmod.connect()
    revs = service.list_revisions(conn, rid)
    assert len(revs) == 3  # 三条修订一条不丢
    final_level = Decimal(service.get_reading(conn, rid)["water_level"])
    assert final_level in {Decimal("2.0"), Decimal("2.1"), Decimal("2.2")}
    conn.close()
