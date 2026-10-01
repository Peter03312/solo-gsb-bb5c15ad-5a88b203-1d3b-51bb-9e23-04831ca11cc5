"""运行期配置，全部来自环境变量，带默认值，便于容器一键启动。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    db_path: str
    # 审核/发布类接口的令牌；为空则不鉴权（仅适合内网演示）
    review_token: str
    # 空数据库时是否自动写入演示站、演示设备及一段会触发预警/解除的读数
    seed_demo: bool
    # 业务时间常量（秒）
    segment_gap_seconds: int = 15 * 60
    rise_min_readings: int = 3
    rise_min_span_seconds: int = 10 * 60
    clear_min_readings: int = 2
    clear_min_span_seconds: int = 5 * 60

    @property
    def db_url(self) -> str:
        return f"file:{self.db_path}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    db_path = os.getenv("FLOOD_DB_PATH", "/data/flood.db")
    # 确保目录存在（容器内 /data 由卷挂载；本地裸跑时自动创建）
    parent = Path(db_path).parent
    if parent and str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    return Settings(
        db_path=db_path,
        review_token=os.getenv("FLOOD_REVIEW_TOKEN", ""),
        seed_demo=_as_bool(os.getenv("FLOOD_SEED_DEMO"), True),
    )
