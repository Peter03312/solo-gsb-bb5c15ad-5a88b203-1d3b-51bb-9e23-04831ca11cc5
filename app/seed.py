"""空库初始化时写入演示数据：一个站、一台设备，
含一次会触发预警并解除的连续过程，以及一个断段后的孤立高读数（不应预警）。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from . import service
from .config import get_settings

DEMO_STATION = "DEMO-001"
DEMO_DEVICE = "DEV-DEMO-0001"


def seed_demo(conn: sqlite3.Connection) -> None:
    service.register_station(
        conn,
        {
            "code": DEMO_STATION,
            "name": "演示站-青石沟",
            "rise_threshold": "5.00",
            "clear_threshold": "4.00",
            "devices": [{"device_no": DEMO_DEVICE, "name": "演示水位计"}],
        },
    )

    # 时间线（相对当前时间向过去取，全部间隔 5 分钟，同属一段）：
    #   正常 3.5, 3.8 -> 连续高位 5.4, 5.8, 6.2（3 个 >=5，跨度 10 分钟，触发预警）
    #   -> 连续低位 3.9, 3.7（2 个 <=4，跨度 5 分钟，解除）
    #   -> 间隔 30 分钟（断段）后单独一个 7.0（孤立，不再次预警）
    base = datetime.now(timezone(timedelta(hours=8)))
    base = base.replace(minute=(base.minute // 5) * 5, second=0, microsecond=0)
    levels = [
        (-40, "3.5"),
        (-35, "3.8"),
        (-30, "5.4"),
        (-25, "5.8"),
        (-20, "6.2"),
        (-15, "3.9"),
        (-10, "3.7"),
        (20, "7.0"),  # 距上一读数 30 分钟 -> 新段，仅 1 个高位读数
    ]
    for seq, (delta_minutes, level) in enumerate(levels):
        ts = (base + timedelta(minutes=delta_minutes)).isoformat()
        service.ingest_reading(
            conn,
            {
                "device_no": DEMO_DEVICE,
                "seq": seq + 1,
                "observed_at": ts,
                "water_level": level,
            },
        )
