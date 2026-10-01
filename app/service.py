"""业务服务层：登记、补传、修订、裁定、发布全部落库操作。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from . import db as dbmod
from .config import Settings, get_settings
from .engine import ReadingPoint, compute_timeline, current_state
from .validation import (
    ValidationError,
    epoch_to_iso,
    parse_observed_at,
    parse_threshold,
    parse_seq,
    parse_water_level,
    validate_threshold_pair,
)


def now_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()


# ---------------------------------------------------------------- 序列化

def station_payload(row: sqlite3.Row) -> dict:
    return {
        "code": row["code"],
        "name": row["name"],
        "rise_threshold": row["rise_threshold"],
        "clear_threshold": row["clear_threshold"],
        "created_at": epoch_to_iso(row["created_at"], 0),
    }


def device_payload(row: sqlite3.Row, station_code: str) -> dict:
    return {
        "device_no": row["device_no"],
        "station_code": station_code,
        "name": row["name"],
        "created_at": epoch_to_iso(row["created_at"], 0),
    }


def reading_payload(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "station_id": row["station_id"],
        "station_code": row["station_code"] if "station_code" in row.keys() else None,
        "device_id": row["device_id"],
        "device_no": row["device_no"] if "device_no" in row.keys() else None,
        "seq": row["seq"],
        "observed_at": epoch_to_iso(row["observed_at"], row["offset_minutes"]),
        "observed_at_epoch": row["observed_at"],
        "water_level": row["water_level"],
        "status": row["status"],
        "ingested_at": epoch_to_iso(row["ingested_at"], 0),
    }


def revision_payload(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "reading_id": row["reading_id"],
        "action": row["action"],
        "old_water_level": row["old_water_level"],
        "new_water_level": row["new_water_level"],
        "reason": row["reason"],
        "reviewer": row["reviewer"],
        "created_at": epoch_to_iso(row["created_at"], 0),
    }


# ---------------------------------------------------------------- 基础查询

def get_station(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM stations WHERE code = ?", (code,)).fetchone()


def require_station(conn: sqlite3.Connection, code: str) -> sqlite3.Row:
    row = get_station(conn, code)
    if row is None:
        raise LookupError(f"未知监测站: {code}")
    return row


def get_device(conn: sqlite3.Connection, device_no: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM devices WHERE device_no = ?", (device_no,)).fetchone()


# ---------------------------------------------------------------- 登记

def register_station(conn: sqlite3.Connection, data: dict) -> dict:
    code = (data.get("code") or "").strip()
    name = (data.get("name") or "").strip()
    if not code:
        raise ValidationError("code 不能为空")
    if not name:
        raise ValidationError("name 不能为空")
    rise = parse_threshold(data.get("rise_threshold"), "rise_threshold")
    clear = parse_threshold(data.get("clear_threshold"), "clear_threshold")
    validate_threshold_pair(rise, clear)

    with dbmod.transaction(conn):
        existing = get_station(conn, code)
        if existing is not None:
            # 幂等：完全相同则返回已存在；阈值不同视为冲突
            same = (
                existing["name"] == name
                and existing["rise_threshold"] == rise
                and existing["clear_threshold"] == clear
            )
            if not same:
                raise ValidationError(f"监测站 {code} 已存在且登记信息不同（如需调整阈值请走站务变更）")
            return station_payload(existing)
        cur = conn.execute(
            "INSERT INTO stations(code, name, rise_threshold, clear_threshold, created_at)"
            " VALUES (?,?,?,?,?)",
            (code, name, rise, clear, now_epoch()),
        )
        station_id = cur.lastrowid
        for dev in data.get("devices") or []:
            device_no = (dev.get("device_no") or "").strip()
            dname = (dev.get("name") or "").strip()
            if not device_no:
                raise ValidationError("devices[].device_no 不能为空")
            conn.execute(
                "INSERT INTO devices(station_id, device_no, name, created_at) VALUES (?,?,?,?)",
                (station_id, device_no, dname or device_no, now_epoch()),
            )
    row = require_station(conn, code)
    return station_payload(row)


def add_device(conn: sqlite3.Connection, station_code: str, data: dict) -> dict:
    device_no = (data.get("device_no") or "").strip()
    name = (data.get("name") or "").strip()
    if not device_no:
        raise ValidationError("device_no 不能为空")
    with dbmod.transaction(conn):
        station = require_station(conn, station_code)
        existing = get_device(conn, device_no)
        if existing is not None:
            if existing["station_id"] == station["id"]:
                row = existing
                return device_payload(row, station_code)
            raise ValidationError(f"设备 {device_no} 已登记到其他监测站")
        conn.execute(
            "INSERT INTO devices(station_id, device_no, name, created_at) VALUES (?,?,?,?)",
            (station["id"], device_no, name or device_no, now_epoch()),
        )
        row = get_device(conn, device_no)
    return device_payload(row, station_code)


def list_stations(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM stations ORDER BY code").fetchall()
    return [station_payload(r) for r in rows]


def list_devices(conn: sqlite3.Connection, station_code: str | None = None) -> list[dict]:
    sql = (
        "SELECT d.*, s.code AS station_code FROM devices d"
        " JOIN stations s ON s.id = d.station_id"
    )
    params: tuple = ()
    if station_code:
        sql += " WHERE s.code = ?"
        params = (station_code,)
    sql += " ORDER BY s.code, d.device_no"
    rows = conn.execute(sql, params).fetchall()
    return [device_payload(r, r["station_code"]) for r in rows]


# ---------------------------------------------------------------- 补传

_READING_SELECT = (
    "SELECT r.*, s.code AS station_code, d.device_no AS device_no"
    " FROM readings r"
    " JOIN devices d ON d.id = r.device_id"
    " JOIN stations s ON s.id = r.station_id"
)


def ingest_reading(conn: sqlite3.Connection, data: dict) -> tuple[bool, dict]:
    """设备补传。返回 (是否新建, 读数载荷)。

    同设备同序号：同内容（观测时间+水位）返回原结果；异内容抛 ValidationError(409)。
    """
    device_no = (data.get("device_no") or "").strip()
    if not device_no:
        raise ValidationError("device_no 不能为空")
    seq = parse_seq(data.get("seq"))
    epoch, offset_minutes = parse_observed_at(data.get("observed_at"))
    water_level = parse_water_level(data.get("water_level"))

    with dbmod.transaction(conn):
        device = get_device(conn, device_no)
        if device is None:
            raise LookupError(f"未知设备: {device_no}")
        existing = conn.execute(
            "SELECT * FROM readings WHERE device_id = ? AND seq = ?",
            (device["id"], seq),
        ).fetchone()
        if existing is not None:
            if (
                existing["observed_at"] == epoch
                and existing["water_level"] == water_level
            ):
                row = conn.execute(
                    _READING_SELECT + " WHERE r.id = ?", (existing["id"],)
                ).fetchone()
                return False, reading_payload(row)
            raise ValidationError(
                "冲突：设备 {} 序号 {} 已存在不同内容（已存 {} @ {}，提交 {} @ {}）".format(
                    device_no,
                    seq,
                    existing["water_level"],
                    epoch_to_iso(existing["observed_at"], existing["offset_minutes"]),
                    water_level,
                    epoch_to_iso(epoch, offset_minutes),
                ),
                )
        cur = conn.execute(
            "INSERT INTO readings(device_id, station_id, seq, observed_at, offset_minutes,"
            " water_level, status, ingested_at) VALUES (?,?,?,?,?,?, 'active', ?)",
            (
                device["id"],
                device["station_id"],
                seq,
                epoch,
                offset_minutes,
                water_level,
                now_epoch(),
            ),
        )
        row = conn.execute(_READING_SELECT + " WHERE r.id = ?", (cur.lastrowid,)).fetchone()
    return True, reading_payload(row)


def list_readings(
    conn: sqlite3.Connection,
    station_code: str,
    device_no: str | None = None,
    status: str | None = "active",
) -> list[dict]:
    station = require_station(conn, station_code)
    sql = _READING_SELECT + " WHERE r.station_id = ?"
    params: list = [station["id"]]
    if device_no:
        device = get_device(conn, device_no)
        if device is None or device["station_id"] != station["id"]:
            raise LookupError(f"该站无此设备: {device_no}")
        sql += " AND r.device_id = ?"
        params.append(device["id"])
    if status != "all":
        if status not in ("active", "voided"):
            raise ValidationError("status 只能是 active、voided 或 all")
        sql += " AND r.status = ?"
        params.append(status)
    sql += " ORDER BY r.observed_at, r.id"
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [reading_payload(r) for r in rows]


def _get_reading_row(conn: sqlite3.Connection, reading_id: int) -> sqlite3.Row:
    row = conn.execute(_READING_SELECT + " WHERE r.id = ?", (reading_id,)).fetchone()
    if row is None:
        raise LookupError(f"未知读数: {reading_id}")
    return row


def get_reading(conn: sqlite3.Connection, reading_id: int) -> dict:
    return reading_payload(_get_reading_row(conn, reading_id))


def list_revisions(conn: sqlite3.Connection, reading_id: int | None = None) -> list[dict]:
    sql = (
        "SELECT rv.* FROM reading_revisions rv"
        + (" WHERE rv.reading_id = ?" if reading_id is not None else "")
        + " ORDER BY rv.id"
    )
    params = (reading_id,) if reading_id is not None else ()
    return [revision_payload(r) for r in conn.execute(sql, params).fetchall()]


# ---------------------------------------------------------------- 审核

def correct_reading(conn: sqlite3.Connection, reading_id: int, data: dict, reviewer: str) -> dict:
    new_level = parse_water_level(data.get("water_level"))
    reason = (data.get("reason") or "").strip()
    if not reason:
        raise ValidationError("修订必须填写 reason")
    with dbmod.transaction(conn):
        row = _get_reading_row(conn, reading_id)
        if row["status"] != "active":
            raise ValidationError(f"读数 {reading_id} 已作废，不能更正（请重新补传）")
        if Decimal(row["water_level"]) == Decimal(new_level):
            raise ValidationError("更正后的水位与原值相同，无需修订")
        conn.execute(
            "INSERT INTO reading_revisions(reading_id, action, old_water_level,"
            " new_water_level, reason, reviewer, created_at) VALUES (?,?,?,?,?,?,?)",
            (reading_id, "correct", row["water_level"], new_level, reason, reviewer, now_epoch()),
        )
        conn.execute("UPDATE readings SET water_level = ? WHERE id = ?", (new_level, reading_id))
        fresh = _get_reading_row(conn, reading_id)
        result = reading_payload(fresh)
    return result


def void_reading(conn: sqlite3.Connection, reading_id: int, data: dict, reviewer: str) -> dict:
    reason = (data.get("reason") or "").strip()
    if not reason:
        raise ValidationError("作废必须填写 reason")
    with dbmod.transaction(conn):
        row = _get_reading_row(conn, reading_id)
        if row["status"] == "voided":
            raise ValidationError(f"读数 {reading_id} 已作废，请勿重复操作")
        conn.execute(
            "INSERT INTO reading_revisions(reading_id, action, old_water_level,"
            " new_water_level, reason, reviewer, created_at) VALUES (?,?,?,?,?,?,?)",
            (reading_id, "void", row["water_level"], None, reason, reviewer, now_epoch()),
        )
        conn.execute("UPDATE readings SET status = 'voided' WHERE id = ?", (reading_id,))
        fresh = _get_reading_row(conn, reading_id)
        result = reading_payload(fresh)
    return result


# ---------------------------------------------------------------- 裁定

def _active_points(conn: sqlite3.Connection, station_id: int) -> list[ReadingPoint]:
    rows = conn.execute(
        "SELECT id, observed_at, water_level FROM readings"
        " WHERE station_id = ? AND status = 'active' ORDER BY observed_at, id",
        (station_id,),
    ).fetchall()
    return [ReadingPoint(id=r["id"], observed_at=r["observed_at"], water_level=r["water_level"]) for r in rows]


def _event_json(event: dict) -> dict:
    return {
        "event": event["event"],
        "first_reading_id": event["first_reading_id"],
        "trigger_reading_id": event["trigger_reading_id"],
        "first_observed_at": epoch_to_iso(event["first_observed_at"], 0),
        "trigger_observed_at": epoch_to_iso(event["trigger_observed_at"], 0),
        "reading_count": event["reading_count"],
        "span_seconds": event["span_seconds"],
        "segment_gap_before": event["segment_gap_before"],
    }


def _segments(points: list[ReadingPoint], gap: int) -> list[dict]:
    segments: list[dict] = []
    if not points:
        return segments
    start = points[0]
    prev = points[0]
    index = 0
    for point in points[1:]:
        if point.observed_at - prev.observed_at > gap:
            segments.append(
                {
                    "index": index,
                    "first_reading_id": start.id,
                    "last_reading_id": prev.id,
                    "start": epoch_to_iso(start.observed_at, 0),
                    "end": epoch_to_iso(prev.observed_at, 0),
                }
            )
            index += 1
            start = point
        prev = point
    segments.append(
        {
            "index": index,
            "first_reading_id": start.id,
            "last_reading_id": prev.id,
            "start": epoch_to_iso(start.observed_at, 0),
            "end": epoch_to_iso(prev.observed_at, 0),
        }
    )
    return segments


def build_timeline_snapshot(conn: sqlite3.Connection, station: sqlite3.Row) -> dict:
    """根据当前有效读数重算待发布时间线（草稿）。"""
    settings: Settings = get_settings()
    points = _active_points(conn, station["id"])
    events = compute_timeline(
        points,
        station["rise_threshold"],
        station["clear_threshold"],
        settings.segment_gap_seconds,
        settings.rise_min_readings,
        settings.rise_min_span_seconds,
        settings.clear_min_readings,
        settings.clear_min_span_seconds,
    )
    return {
        "station_code": station["code"],
        "station_name": station["name"],
        "thresholds": {
            "rise": station["rise_threshold"],
            "clear": station["clear_threshold"],
        },
        "state": current_state(events),
        "events": [_event_json(e) for e in events],
        "segments": _segments(points, settings.segment_gap_seconds),
        "readings": [
            {
                "id": p.id,
                "observed_at": epoch_to_iso(p.observed_at, 0),
                "water_level": p.water_level,
            }
            for p in points
        ],
        "generated_at": epoch_to_iso(now_epoch(), 0),
    }


def get_draft_timeline(conn: sqlite3.Connection, station_code: str) -> dict:
    station = require_station(conn, station_code)
    snapshot = build_timeline_snapshot(conn, station)
    latest = conn.execute(
        "SELECT payload FROM publications WHERE station_id = ? ORDER BY version DESC LIMIT 1",
        (station["id"],),
    ).fetchone()
    in_sync = latest is not None and latest["payload"] == json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True
    )
    snapshot["published"] = latest is not None
    snapshot["in_sync_with_public"] = bool(in_sync)
    return snapshot


# ---------------------------------------------------------------- 发布

def publish_timelines(conn: sqlite3.Connection, station_codes: list[str] | None, publisher: str) -> list[dict]:
    """冻结各站当前时间线为新版本；一次事务，保证与补传/修订串行。"""
    results: list[dict] = []
    with dbmod.transaction(conn):
        if station_codes:
            codes = sorted({c.strip() for c in station_codes if c.strip()})
            if not codes:
                raise ValidationError("stations 不能为空")
        else:
            codes = [r["code"] for r in conn.execute("SELECT code FROM stations ORDER BY code").fetchall()]
        for code in codes:
            station = require_station(conn, code)
            snapshot = build_timeline_snapshot(conn, station)
            version_row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM publications WHERE station_id = ?",
                (station["id"],),
            ).fetchone()
            version = version_row["v"] + 1
            payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
            created = now_epoch()
            conn.execute(
                "INSERT INTO publications(station_id, version, payload, created_at, publisher)"
                " VALUES (?,?,?,?,?)",
                (station["id"], version, payload, created, publisher),
            )
            snapshot["version"] = version
            snapshot["published_at"] = epoch_to_iso(created, 0)
            snapshot["publisher"] = publisher
            results.append(snapshot)
    return results


def list_publications(conn: sqlite3.Connection, station_code: str) -> list[dict]:
    station = require_station(conn, station_code)
    rows = conn.execute(
        "SELECT id, version, payload, created_at, publisher FROM publications"
        " WHERE station_id = ? ORDER BY version",
        (station["id"],),
    ).fetchall()
    out = []
    for row in rows:
        payload = json.loads(row["payload"])
        out.append(
            {
                "version": row["version"],
                "state": payload["state"],
                "event_count": len(payload["events"]),
                "published_at": epoch_to_iso(row["created_at"], 0),
                "publisher": row["publisher"],
            }
        )
    return out


def get_public_timeline(conn: sqlite3.Connection, station_code: str, version: int | None = None) -> dict:
    station = require_station(conn, station_code)
    if version is None:
        row = conn.execute(
            "SELECT * FROM publications WHERE station_id = ? ORDER BY version DESC LIMIT 1",
            (station["id"],),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM publications WHERE station_id = ? AND version = ?",
            (station["id"], version),
        ).fetchone()
    if row is None:
        raise LookupError(
            f"站点 {station_code} 尚无可公开的发布版本"
            + (f"（version={version}）" if version is not None else "")
        )
    payload = json.loads(row["payload"])
    payload["version"] = row["version"]
    payload["published_at"] = epoch_to_iso(row["created_at"], 0)
    payload["publisher"] = row["publisher"]
    return payload


def list_public_stations(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT s.code, s.name, p.version, p.payload, p.created_at"
        " FROM (SELECT station_id, MAX(version) AS mv FROM publications GROUP BY station_id) m"
        " JOIN publications p ON p.station_id = m.station_id AND p.version = m.mv"
        " JOIN stations s ON s.id = p.station_id"
        " ORDER BY s.code"
    ).fetchall()
    out = []
    for row in rows:
        payload = json.loads(row["payload"])
        out.append(
            {
                "code": row["code"],
                "name": row["name"],
                "version": row["version"],
                "state": payload["state"],
                "last_event": payload["events"][-1] if payload["events"] else None,
                "published_at": epoch_to_iso(row["created_at"], 0),
            }
        )
    return out
