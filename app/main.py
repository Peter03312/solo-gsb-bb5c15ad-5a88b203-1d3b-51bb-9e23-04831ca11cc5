"""FastAPI 入口：山洪水位预警裁定服务 HTTP API。"""
from __future__ import annotations

import os
import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from . import db as dbmod
from . import service
from .config import get_settings
from .schemas import CorrectIn, DeviceAddIn, PublishIn, ReadingIn, StationIn, VoidIn
from .validation import ValidationError


def get_conn(request: Request) -> sqlite3.Connection:
    return request.app.state.db


def require_reviewer(x_reviewer: str | None = Header(default=None)) -> str:
    return (x_reviewer or "").strip() or "anonymous-reviewer"


def require_admin(x_reviewer_token: str | None = Header(default=None)) -> str:
    token = get_settings().review_token
    if token and x_reviewer_token != token:
        raise HTTPException(status_code=401, detail="审核/发布接口需要 X-Reviewer-Token")
    return "reviewer"


Reviewer = Depends(require_reviewer)
Admin = Depends(require_admin)


def _seed_if_empty(conn: sqlite3.Connection) -> None:
    settings = get_settings()
    if not settings.seed_demo or dbmod.database_exists_with_data(conn):
        return
    try:
        from .seed import seed_demo

        seed_demo(conn)
    except Exception as exc:  # 种子失败不影响服务启动
        print(f"[startup] demo seed skipped: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    conn = dbmod.connect(settings.db_path)
    dbmod.init_db(conn)
    _seed_if_empty(conn)
    app.state.db = conn
    yield
    conn.close()


app = FastAPI(
    title="山洪水位预警裁定服务",
    version="1.0.0",
    description=(
        "监测站阈值/设备登记、设备补传（幂等/冲突）、按观测时间重排的预警/解除裁定、"
        "审核作废/更正、发布冻结与公开读取。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(ValidationError)
async def validation_error_handler(request: Request, exc: ValidationError) -> JSONResponse:
    # 文本以“冲突：”开头的校验错误对应 409（设备同序号异内容等）
    code = 409 if exc.message.startswith("冲突") else 422
    return JSONResponse(status_code=code, content={"detail": exc.message})


@app.exception_handler(LookupError)
async def lookup_error_handler(request: Request, exc: LookupError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


# ---------------------------------------------------------------- 健康检查

@app.get("/health", tags=["system"])
def health(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    conn.execute("SELECT 1").fetchone()
    return {"status": "ok"}


# ---------------------------------------------------------------- 登记

@app.post("/api/stations", status_code=status.HTTP_201_CREATED, tags=["registry"], dependencies=[Admin])
def create_station(body: StationIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return service.register_station(conn, body.model_dump())


@app.get("/api/stations", tags=["registry"])
def stations(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return {"stations": service.list_stations(conn)}


@app.get("/api/stations/{station_code}", tags=["registry"])
def station_detail(station_code: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    row = service.require_station(conn, station_code)
    return service.station_payload(row)


@app.post("/api/stations/{station_code}/devices", status_code=status.HTTP_201_CREATED,
          tags=["registry"], dependencies=[Admin])
def add_device(station_code: str, body: DeviceAddIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return service.add_device(conn, station_code, body.model_dump())


@app.get("/api/devices", tags=["registry"])
def devices(station_code: str | None = None, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return {"devices": service.list_devices(conn, station_code)}


# ---------------------------------------------------------------- 设备补传

@app.post("/api/readings", tags=["ingestion"])
def ingest_reading(body: ReadingIn, conn: sqlite3.Connection = Depends(get_conn)) -> JSONResponse:
    created, payload = service.ingest_reading(conn, body.model_dump())
    payload["replayed"] = not created
    return JSONResponse(
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        content=payload,
    )


@app.get("/api/stations/{station_code}/readings", tags=["ingestion"])
def station_readings(
    station_code: str,
    device_no: str | None = None,
    status_filter: str = Query(default="active", alias="status"),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    return {"readings": service.list_readings(conn, station_code, device_no, status_filter)}


@app.get("/api/readings/{reading_id}", tags=["review"])
def reading_detail(reading_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return service.get_reading(conn, reading_id)


# ---------------------------------------------------------------- 审核

@app.post("/api/readings/{reading_id}/correct", tags=["review"], dependencies=[Admin])
def correct_reading(
    reading_id: int,
    body: CorrectIn,
    conn: sqlite3.Connection = Depends(get_conn),
    reviewer: str = Reviewer,
) -> dict:
    return service.correct_reading(conn, reading_id, body.model_dump(), reviewer)


@app.post("/api/readings/{reading_id}/void", tags=["review"], dependencies=[Admin])
def void_reading(
    reading_id: int,
    body: VoidIn,
    conn: sqlite3.Connection = Depends(get_conn),
    reviewer: str = Reviewer,
) -> dict:
    return service.void_reading(conn, reading_id, body.model_dump(), reviewer)


@app.get("/api/readings/{reading_id}/revisions", tags=["review"])
def reading_revisions(reading_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    service.get_reading(conn, reading_id)  # 不存在则 404
    return {"revisions": service.list_revisions(conn, reading_id)}


# ---------------------------------------------------------------- 待发布裁定

@app.get("/api/stations/{station_code}/timeline", tags=["adjudication"])
def draft_timeline(station_code: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return service.get_draft_timeline(conn, station_code)


@app.post("/api/publish", status_code=status.HTTP_201_CREATED, tags=["publishing"], dependencies=[Admin])
def publish(
    body: PublishIn,
    conn: sqlite3.Connection = Depends(get_conn),
    reviewer: str = Reviewer,
) -> dict:
    snapshots = service.publish_timelines(conn, body.stations, reviewer)
    return {"published": snapshots, "count": len(snapshots)}


@app.get("/api/stations/{station_code}/publications", tags=["publishing"])
def publications(station_code: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return {"publications": service.list_publications(conn, station_code)}


# ---------------------------------------------------------------- 公开读取

@app.get("/public/stations", tags=["public"])
def public_stations(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return {"stations": service.list_public_stations(conn)}


@app.get("/public/stations/{station_code}/timeline", tags=["public"])
def public_timeline(
    station_code: str, version: int | None = None, conn: sqlite3.Connection = Depends(get_conn)
) -> dict:
    return service.get_public_timeline(conn, station_code, version)
