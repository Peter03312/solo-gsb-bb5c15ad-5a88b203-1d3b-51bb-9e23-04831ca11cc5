"""山洪水位预警裁定服务 —— FastAPI 入口。

运行：uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .adjudication import Reading, Rules, adjudicate
from .config import Settings, load_settings
from .db import Database
from .schemas import (
    CorrectRequest,
    DeviceCreate,
    PublishRequest,
    ReadingIngest,
    StationCreate,
    VoidRequest,
)


# ---------------------------------------------------------------- 工具函数

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_stored(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def reading_to_dict(row: sqlite3.Row, device_no: str) -> dict[str, Any]:
    return {
        "id": row["id"],
        "device_no": device_no,
        "seq": row["seq"],
        "observed_at": iso_z(parse_stored(row["observed_at"])),
        "water_level": row["water_level"],
        "original_observed_at": iso_z(parse_stored(row["original_observed_at"])),
        "original_water_level": row["original_water_level"],
        "status": row["status"],
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def revision_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    def ts(v: Optional[str]) -> Optional[str]:
        return iso_z(parse_stored(v)) if v is not None else None

    return {
        "revision_no": row["revision_no"],
        "action": row["action"],
        "old_observed_at": ts(row["old_observed_at"]),
        "old_water_level": row["old_water_level"],
        "new_observed_at": ts(row["new_observed_at"]),
        "new_water_level": row["new_water_level"],
        "reason": row["reason"],
        "auditor": row["auditor"],
        "created_at": row["created_at"],
    }


def _safe_json(obj: Any) -> Any:
    """保证可 JSON 序列化：NaN/Inf 转字符串，未知类型经 default=str 兜底。"""
    def walk(o: Any) -> Any:
        if isinstance(o, float) and not math.isfinite(o):
            return repr(o)
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [walk(v) for v in o]
        return o

    return json.loads(json.dumps(walk(obj), default=str))


# ---------------------------------------------------------------- 应用工厂

def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or load_settings()
    db = Database(settings.db_path)
    rules = Rules(
        segment_gap=timedelta(minutes=settings.segment_gap_minutes),
        rise_min_count=settings.rise_min_count,
        rise_min_span=timedelta(minutes=settings.rise_min_span_minutes),
        clear_min_count=settings.clear_min_count,
        clear_min_span=timedelta(minutes=settings.clear_min_span_minutes),
    )

    app = FastAPI(
        title="山洪水位预警裁定服务",
        version="1.0.0",
        description="监测站/设备登记、读数补传（幂等）、审核修订、预警裁定与发布冻结。",
    )
    app.state.db = db
    app.state.settings = settings

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        # 非法时间 / 水位 / 阈值等一律返回 400
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": "请求参数非法", "errors": _safe_json(exc.errors())},
        )

    # ------------------------------------------------------------ 裁定计算

    def compute_timeline(conn: sqlite3.Connection, station_id: int) -> dict[str, Any]:
        st = conn.execute("SELECT * FROM stations WHERE id=?", (station_id,)).fetchone()
        if st is None:
            raise HTTPException(status_code=404, detail=f"监测站 {station_id} 不存在")
        rows = conn.execute(
            """
            SELECT r.id, r.observed_at, r.water_level
            FROM readings r
            JOIN devices d ON d.id = r.device_id
            WHERE d.station_id = ? AND r.status = 'active'
            """,
            (station_id,),
        ).fetchall()
        readings = [
            Reading(id=r["id"], observed_at=parse_stored(r["observed_at"]), water_level=r["water_level"])
            for r in rows
        ]
        res = adjudicate(readings, st["rise_threshold"], st["clear_threshold"], rules)
        return {
            "station_id": station_id,
            "station_name": st["name"],
            "rise_threshold": st["rise_threshold"],
            "clear_threshold": st["clear_threshold"],
            "status": res.status,
            "active_warning_since": iso_z(res.active_warning_since) if res.active_warning_since else None,
            "readings_considered": res.readings_considered,
            "events": [
                {
                    "type": e.type,
                    "at": iso_z(e.at),
                    "reading_id": e.reading_id,
                    "run_start": iso_z(e.run_start),
                    "run_count": e.run_count,
                    "span_seconds": e.span_seconds,
                }
                for e in res.events
            ],
            "segments": [
                {"start": iso_z(s["start"]), "end": iso_z(s["end"]), "count": s["count"]}
                for s in res.segments
            ],
        }

    def get_station_or_404(conn: sqlite3.Connection, station_id: int) -> sqlite3.Row:
        st = conn.execute("SELECT * FROM stations WHERE id=?", (station_id,)).fetchone()
        if st is None:
            raise HTTPException(status_code=404, detail=f"监测站 {station_id} 不存在")
        return st

    def get_reading_or_404(conn: sqlite3.Connection, reading_id: int) -> tuple[sqlite3.Row, str]:
        row = conn.execute(
            """
            SELECT r.*, d.device_no AS device_no
            FROM readings r JOIN devices d ON d.id = r.device_id
            WHERE r.id = ?
            """,
            (reading_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"读数 {reading_id} 不存在")
        return row, row["device_no"]

    def check_water_level(level: float) -> None:
        if not (settings.min_water_level <= level <= settings.max_water_level):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"水位 {level} 超出合法范围 "
                    f"[{settings.min_water_level}, {settings.max_water_level}]"
                ),
            )

    # ------------------------------------------------------------ 监测站 / 设备

    @app.post("/stations", status_code=201, tags=["监测站"])
    def create_station(body: StationCreate) -> dict[str, Any]:
        now = iso_z(utc_now())
        with db.write_tx() as conn:
            cur = conn.execute(
                "INSERT INTO stations(name, rise_threshold, clear_threshold, created_at) VALUES (?,?,?,?)",
                (body.name, body.rise_threshold, body.clear_threshold, now),
            )
            row = conn.execute("SELECT * FROM stations WHERE id=?", (cur.lastrowid,)).fetchone()
        return {
            "id": row["id"],
            "name": row["name"],
            "rise_threshold": row["rise_threshold"],
            "clear_threshold": row["clear_threshold"],
            "created_at": row["created_at"],
        }

    @app.get("/stations", tags=["监测站"])
    def list_stations() -> list[dict[str, Any]]:
        with db.read() as conn:
            rows = conn.execute("SELECT * FROM stations ORDER BY id").fetchall()
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "rise_threshold": r["rise_threshold"],
                "clear_threshold": r["clear_threshold"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    @app.get("/stations/{station_id}", tags=["监测站"])
    def get_station(station_id: int) -> dict[str, Any]:
        with db.read() as conn:
            st = get_station_or_404(conn, station_id)
            devices = conn.execute(
                "SELECT * FROM devices WHERE station_id=? ORDER BY id", (station_id,)
            ).fetchall()
        return {
            "id": st["id"],
            "name": st["name"],
            "rise_threshold": st["rise_threshold"],
            "clear_threshold": st["clear_threshold"],
            "created_at": st["created_at"],
            "devices": [
                {"id": d["id"], "device_no": d["device_no"], "created_at": d["created_at"]}
                for d in devices
            ],
        }

    @app.post("/stations/{station_id}/devices", status_code=201, tags=["设备"])
    def create_device(station_id: int, body: DeviceCreate) -> dict[str, Any]:
        now = iso_z(utc_now())
        with db.write_tx() as conn:
            get_station_or_404(conn, station_id)
            try:
                cur = conn.execute(
                    "INSERT INTO devices(device_no, station_id, created_at) VALUES (?,?,?)",
                    (body.device_no, station_id, now),
                )
            except sqlite3.IntegrityError:
                raise HTTPException(status_code=409, detail=f"设备号 {body.device_no} 已存在")
            row = conn.execute("SELECT * FROM devices WHERE id=?", (cur.lastrowid,)).fetchone()
        return {
            "id": row["id"],
            "device_no": row["device_no"],
            "station_id": row["station_id"],
            "created_at": row["created_at"],
        }

    @app.get("/stations/{station_id}/devices", tags=["设备"])
    def list_devices(station_id: int) -> list[dict[str, Any]]:
        with db.read() as conn:
            get_station_or_404(conn, station_id)
            rows = conn.execute(
                "SELECT * FROM devices WHERE station_id=? ORDER BY id", (station_id,)
            ).fetchall()
        return [
            {"id": r["id"], "device_no": r["device_no"], "station_id": r["station_id"], "created_at": r["created_at"]}
            for r in rows
        ]

    # ------------------------------------------------------------ 读数补传（幂等）

    @app.post("/readings", tags=["读数"])
    def ingest_reading(body: ReadingIngest, response: Response) -> dict[str, Any]:
        check_water_level(body.water_level)
        obs_utc = body.observed_at.astimezone(timezone.utc)
        obs_str = obs_utc.isoformat()
        now = iso_z(utc_now())

        with db.write_tx() as conn:
            dev = conn.execute("SELECT * FROM devices WHERE device_no=?", (body.device_no,)).fetchone()
            if dev is None:
                raise HTTPException(status_code=404, detail=f"未知设备号 {body.device_no}")

            existing = conn.execute(
                "SELECT * FROM readings WHERE device_id=? AND seq=?", (dev["id"], body.seq)
            ).fetchone()
            if existing is not None:
                same = (
                    parse_stored(existing["observed_at"]) == obs_utc
                    and existing["water_level"] == body.water_level
                )
                if same:
                    # 同号同序号同内容：返回原结果
                    response.status_code = status.HTTP_200_OK
                    return {**reading_to_dict(existing, dev["device_no"]), "deduplicated": True}
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": f"设备 {body.device_no} 序号 {body.seq} 已存在且内容不一致",
                        "existing": reading_to_dict(existing, dev["device_no"]),
                        "submitted": {
                            "observed_at": iso_z(obs_utc),
                            "water_level": body.water_level,
                        },
                    },
                )
            try:
                cur = conn.execute(
                    """
                    INSERT INTO readings(device_id, seq, observed_at, water_level,
                                         original_observed_at, original_water_level,
                                         status, version, created_at, updated_at)
                    VALUES (?,?,?,?,?,?, 'active', 1, ?, ?)
                    """,
                    (dev["id"], body.seq, obs_str, body.water_level, obs_str, body.water_level, now, now),
                )
            except sqlite3.IntegrityError:
                # 并发下唯一约束兜底：按已存在处理，重新读取后比较
                existing = conn.execute(
                    "SELECT * FROM readings WHERE device_id=? AND seq=?", (dev["id"], body.seq)
                ).fetchone()
                if existing is not None and parse_stored(existing["observed_at"]) == obs_utc \
                        and existing["water_level"] == body.water_level:
                    response.status_code = status.HTTP_200_OK
                    return {**reading_to_dict(existing, dev["device_no"]), "deduplicated": True}
                raise HTTPException(status_code=409, detail=f"设备 {body.device_no} 序号 {body.seq} 冲突")
            row = conn.execute("SELECT * FROM readings WHERE id=?", (cur.lastrowid,)).fetchone()

        response.status_code = status.HTTP_201_CREATED
        return {**reading_to_dict(row, dev["device_no"]), "deduplicated": False}

    @app.get("/stations/{station_id}/readings", tags=["读数"])
    def list_station_readings(station_id: int, include_voided: bool = False) -> list[dict[str, Any]]:
        with db.read() as conn:
            get_station_or_404(conn, station_id)
            sql = """
                SELECT r.*, d.device_no AS device_no
                FROM readings r JOIN devices d ON d.id = r.device_id
                WHERE d.station_id = ?
            """
            if not include_voided:
                sql += " AND r.status = 'active'"
            sql += " ORDER BY r.observed_at, r.id"
            rows = conn.execute(sql, (station_id,)).fetchall()
        return [reading_to_dict(r, r["device_no"]) for r in rows]

    @app.get("/readings/{reading_id}", tags=["读数"])
    def get_reading(reading_id: int) -> dict[str, Any]:
        with db.read() as conn:
            row, device_no = get_reading_or_404(conn, reading_id)
        return reading_to_dict(row, device_no)

    # ------------------------------------------------------------ 审核：作废 / 更正 / 修订历史

    @app.post("/readings/{reading_id}/void", tags=["审核"])
    def void_reading(reading_id: int, body: VoidRequest) -> dict[str, Any]:
        now = iso_z(utc_now())
        with db.write_tx() as conn:
            row, device_no = get_reading_or_404(conn, reading_id)
            if row["status"] == "voided":
                raise HTTPException(status_code=409, detail=f"读数 {reading_id} 已作废")
            conn.execute(
                """
                INSERT INTO reading_revisions(reading_id, revision_no, action,
                                              old_observed_at, old_water_level,
                                              new_observed_at, new_water_level,
                                              reason, auditor, created_at)
                VALUES (?,?,?,?,?, NULL, NULL, ?,?,?)
                """,
                (
                    reading_id, row["version"], "void",
                    row["observed_at"], row["water_level"],
                    body.reason, body.auditor, now,
                ),
            )
            conn.execute(
                "UPDATE readings SET status='voided', version=version+1, updated_at=? WHERE id=?",
                (now, reading_id),
            )
            updated, _ = get_reading_or_404(conn, reading_id)
            station_id = conn.execute(
                "SELECT station_id FROM devices WHERE id=?", (updated["device_id"],)
            ).fetchone()["station_id"]
            timeline = compute_timeline(conn, station_id)
        return {
            "reading": reading_to_dict(updated, device_no),
            "pending_adjudication": {"station_id": station_id, "status": timeline["status"], "events": timeline["events"]},
        }

    @app.post("/readings/{reading_id}/correct", tags=["审核"])
    def correct_reading(reading_id: int, body: CorrectRequest) -> dict[str, Any]:
        if body.water_level is not None:
            check_water_level(body.water_level)
        now = iso_z(utc_now())
        with db.write_tx() as conn:
            row, device_no = get_reading_or_404(conn, reading_id)
            if row["status"] == "voided":
                raise HTTPException(status_code=409, detail=f"读数 {reading_id} 已作废，不能更正")
            new_obs = (
                body.observed_at.astimezone(timezone.utc).isoformat()
                if body.observed_at is not None
                else row["observed_at"]
            )
            new_level = body.water_level if body.water_level is not None else row["water_level"]
            conn.execute(
                """
                INSERT INTO reading_revisions(reading_id, revision_no, action,
                                              old_observed_at, old_water_level,
                                              new_observed_at, new_water_level,
                                              reason, auditor, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    reading_id, row["version"], "correct",
                    row["observed_at"], row["water_level"],
                    new_obs, new_level,
                    body.reason, body.auditor, now,
                ),
            )
            conn.execute(
                "UPDATE readings SET observed_at=?, water_level=?, version=version+1, updated_at=? WHERE id=?",
                (new_obs, new_level, now, reading_id),
            )
            updated, _ = get_reading_or_404(conn, reading_id)
            station_id = conn.execute(
                "SELECT station_id FROM devices WHERE id=?", (updated["device_id"],)
            ).fetchone()["station_id"]
            timeline = compute_timeline(conn, station_id)
        return {
            "reading": reading_to_dict(updated, device_no),
            "pending_adjudication": {"station_id": station_id, "status": timeline["status"], "events": timeline["events"]},
        }

    @app.get("/readings/{reading_id}/revisions", tags=["审核"])
    def list_revisions(reading_id: int) -> dict[str, Any]:
        with db.read() as conn:
            row, device_no = get_reading_or_404(conn, reading_id)
            revs = conn.execute(
                "SELECT * FROM reading_revisions WHERE reading_id=? ORDER BY revision_no",
                (reading_id,),
            ).fetchall()
        return {
            "reading": reading_to_dict(row, device_no),
            "original": {
                "observed_at": iso_z(parse_stored(row["original_observed_at"])),
                "water_level": row["original_water_level"],
            },
            "revisions": [revision_to_dict(r) for r in revs],
        }

    # ------------------------------------------------------------ 裁定时间线（待发布）

    @app.get("/stations/{station_id}/timeline", tags=["裁定"])
    def pending_timeline(station_id: int) -> dict[str, Any]:
        with db.read() as conn:
            timeline = compute_timeline(conn, station_id)
        return {"published": False, **timeline}

    # ------------------------------------------------------------ 发布（冻结公开结果）

    @app.post("/publish", status_code=201, tags=["发布"])
    def publish(body: Optional[PublishRequest] = None) -> dict[str, Any]:
        note = body.note if body else None
        now = iso_z(utc_now())
        with db.write_tx() as conn:
            cur = conn.execute(
                "INSERT INTO publications(published_at, note) VALUES (?,?)", (now, note)
            )
            pub_id = cur.lastrowid
            stations = conn.execute("SELECT id FROM stations ORDER BY id").fetchall()
            summaries = []
            for st in stations:
                timeline = compute_timeline(conn, st["id"])
                conn.execute(
                    "INSERT INTO publication_timelines(publication_id, station_id, timeline_json) VALUES (?,?,?)",
                    (pub_id, st["id"], json.dumps(timeline, ensure_ascii=False)),
                )
                summaries.append(
                    {
                        "station_id": st["id"],
                        "station_name": timeline["station_name"],
                        "status": timeline["status"],
                        "event_count": len(timeline["events"]),
                    }
                )
        return {"publication_id": pub_id, "published_at": now, "note": note, "stations": summaries}

    @app.get("/publications", tags=["发布"])
    def list_publications() -> list[dict[str, Any]]:
        with db.read() as conn:
            rows = conn.execute("SELECT * FROM publications ORDER BY id DESC").fetchall()
        return [{"publication_id": r["id"], "published_at": r["published_at"], "note": r["note"]} for r in rows]

    @app.get("/publications/{publication_id}", tags=["发布"])
    def get_publication(publication_id: int) -> dict[str, Any]:
        with db.read() as conn:
            pub = conn.execute("SELECT * FROM publications WHERE id=?", (publication_id,)).fetchone()
            if pub is None:
                raise HTTPException(status_code=404, detail=f"发布 {publication_id} 不存在")
            rows = conn.execute(
                "SELECT station_id, timeline_json FROM publication_timelines WHERE publication_id=? ORDER BY station_id",
                (publication_id,),
            ).fetchall()
        return {
            "publication_id": pub["id"],
            "published_at": pub["published_at"],
            "note": pub["note"],
            "timelines": [
                {"published": True, **json.loads(r["timeline_json"])} for r in rows
            ],
        }

    @app.get("/public/current", tags=["公开结果"])
    def public_current() -> dict[str, Any]:
        with db.read() as conn:
            pub = conn.execute("SELECT * FROM publications ORDER BY id DESC LIMIT 1").fetchone()
            if pub is None:
                raise HTTPException(status_code=404, detail="尚未发布任何结果")
            rows = conn.execute(
                "SELECT station_id, timeline_json FROM publication_timelines WHERE publication_id=? ORDER BY station_id",
                (pub["id"],),
            ).fetchall()
        return {
            "publication_id": pub["id"],
            "published_at": pub["published_at"],
            "note": pub["note"],
            "timelines": [{"published": True, **json.loads(r["timeline_json"])} for r in rows],
        }

    @app.get("/public/stations/{station_id}/timeline", tags=["公开结果"])
    def public_station_timeline(station_id: int) -> dict[str, Any]:
        with db.read() as conn:
            get_station_or_404(conn, station_id)
            row = conn.execute(
                """
                SELECT pt.timeline_json, p.id AS pub_id, p.published_at
                FROM publication_timelines pt
                JOIN publications p ON p.id = pt.publication_id
                WHERE pt.station_id=?
                ORDER BY pt.publication_id DESC LIMIT 1
                """,
                (station_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="该监测站尚无已发布结果")
        return {
            "publication_id": row["pub_id"],
            "published_at": row["published_at"],
            "published": True,
            **json.loads(row["timeline_json"]),
        }

    # ------------------------------------------------------------ 健康检查

    @app.get("/health", tags=["系统"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
