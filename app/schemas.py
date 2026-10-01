"""HTTP 请求体模型。基础类型校验由 Pydantic 负责，语义校验在 validation/service 层。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class DeviceIn(BaseModel):
    device_no: str
    name: str | None = None


class StationIn(BaseModel):
    code: str
    name: str
    rise_threshold: Any
    clear_threshold: Any
    devices: list[DeviceIn] | None = None


class DeviceAddIn(BaseModel):
    device_no: str
    name: str | None = None


class ReadingIn(BaseModel):
    device_no: str
    seq: int = Field(..., ge=0)
    observed_at: str
    water_level: Any  # 接受数字或字符串，统一由 Decimal 精确解析


class CorrectIn(BaseModel):
    water_level: Any
    reason: str


class VoidIn(BaseModel):
    reason: str


class PublishIn(BaseModel):
    stations: list[str] | None = None  # 为空表示发布全部站点
