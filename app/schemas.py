"""请求体模型与校验。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic import FiniteFloat


def _require_tz_aware(v: datetime) -> datetime:
    if v.tzinfo is None or v.utcoffset() is None:
        raise ValueError("必须携带时区偏移（如 2026-10-01T08:00:00+08:00 或 ...Z）")
    return v


class StationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    rise_threshold: FiniteFloat = Field(description="上升阈值")
    clear_threshold: FiniteFloat = Field(description="解除阈值，必须低于上升阈值")

    @model_validator(mode="after")
    def _check_thresholds(self) -> "StationCreate":
        if not self.rise_threshold > self.clear_threshold:
            raise ValueError("上升阈值必须严格大于解除阈值")
        return self


class DeviceCreate(BaseModel):
    device_no: str = Field(min_length=1, max_length=100, description="设备号（全局唯一）")


class ReadingIngest(BaseModel):
    device_no: str = Field(min_length=1, max_length=100)
    seq: int = Field(ge=0, description="设备内递增序号")
    observed_at: datetime = Field(description="带时区的观测时间")
    water_level: FiniteFloat = Field(description="水位")

    _tz = field_validator("observed_at")(_require_tz_aware)


class VoidRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500, description="作废原因（必填）")
    auditor: Optional[str] = Field(default=None, max_length=100, description="审核员")


class CorrectRequest(BaseModel):
    observed_at: Optional[datetime] = None
    water_level: Optional[FiniteFloat] = None
    reason: str = Field(min_length=1, max_length=500, description="更正原因（必填）")
    auditor: Optional[str] = Field(default=None, max_length=100)

    _tz = field_validator("observed_at")(_require_tz_aware)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "CorrectRequest":
        if self.observed_at is None and self.water_level is None:
            raise ValueError("至少更正 observed_at 或 water_level 之一")
        return self


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    note: Optional[str] = Field(default=None, max_length=500)
