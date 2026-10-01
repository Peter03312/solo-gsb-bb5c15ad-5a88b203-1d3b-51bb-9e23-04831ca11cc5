"""输入校验：带时区的观测时间、水位、阈值、序号。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

# 合理物理范围（米），防止明显的脏数据
_WL_MIN = Decimal("0")
_WL_MAX = Decimal("100000")
_THRESHOLD_MIN = Decimal("0")
_THRESHOLD_MAX = Decimal("100000")


class ValidationError(Exception):
    """422 类业务校验错误。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def parse_observed_at(raw: str) -> tuple[float, int]:
    """解析带时区的 ISO-8601 时间。

    返回 (epoch_seconds, 原始 UTC 偏移分钟数)。
    拒绝：无法解析、不带时区、未来时间（允许 5 分钟时钟偏差）、过早时间。
    时间统一按微秒取整，保证 "同一时刻不同写法" 可判等。
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError("observed_at 必须是非空 ISO-8601 字符串")
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"observed_at 不是合法的 ISO-8601 时间: {raw!r}") from exc
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValidationError("observed_at 必须带时区信息（如 2026-10-01T08:00:00+08:00 或 Z）")
    epoch = round(dt.astimezone(timezone.utc).timestamp(), 6)
    offset_minutes = int(dt.utcoffset().total_seconds() // 60)  # type: ignore[union-attr]
    now = datetime.now(timezone.utc).timestamp()
    if epoch > now + 300:
        raise ValidationError("observed_at 不能是未来时间（允许 5 分钟时钟偏差）")
    if epoch < 946684800:  # 2000-01-01T00:00:00Z
        raise ValidationError("observed_at 过早，必须晚于 2000-01-01T00:00:00Z")
    return epoch, offset_minutes


def parse_water_level(raw) -> str:
    """水位校验，返回规范化十进制字符串（精确、可判等）。"""
    if isinstance(raw, bool):
        raise ValidationError("water_level 必须是数字")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"water_level 不是合法数字: {raw!r}") from exc
    if not value.is_finite():
        raise ValidationError("water_level 必须是有限数字（不允许 NaN/Infinity）")
    # 去掉多余前导零，但保留原始小数位；统一为定点写法
    normalized = format(value, "f")
    value = Decimal(normalized)
    if value < _WL_MIN or value > _WL_MAX:
        raise ValidationError(f"water_level 超出允许范围 [{_WL_MIN}, {_WL_MAX}] 米")
    return normalized


def parse_threshold(raw, field: str = "threshold") -> str:
    """阈值校验。"""
    if isinstance(raw, bool):
        raise ValidationError(f"{field} 必须是数字")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{field} 不是合法数字: {raw!r}") from exc
    if not value.is_finite():
        raise ValidationError(f"{field} 必须是有限数字（不允许 NaN/Infinity）")
    normalized = format(value, "f")
    value = Decimal(normalized)
    if value < _THRESHOLD_MIN or value > _THRESHOLD_MAX:
        raise ValidationError(f"{field} 超出允许范围 [{_THRESHOLD_MIN}, {_THRESHOLD_MAX}] 米")
    return normalized


def validate_threshold_pair(rise: str, clear: str) -> None:
    """解除阈值必须低于上升阈值。"""
    if Decimal(clear) >= Decimal(rise):
        raise ValidationError(f"解除阈值({clear})必须低于上升阈值({rise})")


def parse_seq(raw) -> int:
    """设备内递增序号：非负整数。"""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValidationError("seq 必须是非负整数")
    if raw < 0:
        raise ValidationError("seq 不能为负数")
    return raw


def epoch_to_iso(epoch: float, offset_minutes: int | None = None) -> str:
    """epoch -> 带时区 ISO-8601；offset_minutes 为空则输出原始提交时携带的 UTC 偏移。"""
    if offset_minutes is None:
        offset_minutes = 0
    tz = timezone.utc if offset_minutes == 0 else timezone(timedelta(minutes=offset_minutes))
    return datetime.fromtimestamp(epoch, tz=tz).isoformat()
