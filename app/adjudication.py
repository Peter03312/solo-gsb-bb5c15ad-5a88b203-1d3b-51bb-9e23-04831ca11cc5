"""山洪水位预警裁定核心逻辑（纯函数，不依赖数据库 / Web 框架）。

裁定规则（默认参数，可通过 Rules 覆盖）：
- 有效读数按观测时间升序重排，相邻两条间隔 > 15 分钟即断段；
- 同一段内连续 >= 3 个读数达到上升阈值（>= rise_threshold），
  且该连续段首尾相隔 >= 10 分钟时，在首次同时满足两项条件的读数处发出预警；
- 预警状态下，连续 >= 2 个读数不高于解除阈值（<= clear_threshold），
  且首尾相隔 >= 5 分钟时解除；
- 断段只重置“上升”计数；预警不因断段自动解除，解除计数可跨段延续。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional


@dataclass(frozen=True)
class Reading:
    """参与裁定的一条有效读数。"""

    id: int
    observed_at: datetime  # 已统一为带时区时间（UTC）
    water_level: float


@dataclass(frozen=True)
class Rules:
    segment_gap: timedelta = timedelta(minutes=15)
    rise_min_count: int = 3
    rise_min_span: timedelta = timedelta(minutes=10)
    clear_min_count: int = 2
    clear_min_span: timedelta = timedelta(minutes=5)


@dataclass
class Event:
    type: str  # "warning" | "cleared"
    at: datetime
    reading_id: int
    run_start: datetime
    run_count: int
    span_seconds: float


@dataclass
class AdjudicationResult:
    status: str  # "normal" | "warning"
    events: List[Event] = field(default_factory=list)
    segments: List[dict] = field(default_factory=list)
    readings_considered: int = 0
    active_warning_since: Optional[datetime] = None


def adjudicate(
    readings: List[Reading],
    rise_threshold: float,
    clear_threshold: float,
    rules: Rules = Rules(),
) -> AdjudicationResult:
    """对单个监测站的有效读数做裁定，返回预警/解除事件时间线。"""
    ordered = sorted(readings, key=lambda r: (r.observed_at, r.id))

    result = AdjudicationResult(status="normal", readings_considered=len(ordered))
    status = "normal"
    rise_run: List[Reading] = []   # 同段内连续达到上升阈值的读数
    clear_run: List[Reading] = []  # 预警后连续不高于解除阈值的读数
    prev: Optional[Reading] = None
    seg_start: Optional[Reading] = None
    seg_count = 0

    def close_segment(last: Reading) -> None:
        assert seg_start is not None
        result.segments.append(
            {
                "start": seg_start.observed_at,
                "end": last.observed_at,
                "count": seg_count,
            }
        )

    for r in ordered:
        if prev is None or (r.observed_at - prev.observed_at) > rules.segment_gap:
            if prev is not None:
                close_segment(prev)
            seg_start = r
            seg_count = 0
            # 断段：上升计数必须同段连续，故重置；
            # 解除计数不清零 —— 断段本身不自动解除预警，后续低水位读数继续累计。
            rise_run = []
        seg_count += 1

        if status == "normal":
            if r.water_level >= rise_threshold:
                rise_run.append(r)
                span = r.observed_at - rise_run[0].observed_at
                if len(rise_run) >= rules.rise_min_count and span >= rules.rise_min_span:
                    result.events.append(
                        Event(
                            type="warning",
                            at=r.observed_at,
                            reading_id=r.id,
                            run_start=rise_run[0].observed_at,
                            run_count=len(rise_run),
                            span_seconds=span.total_seconds(),
                        )
                    )
                    status = "warning"
                    rise_run = []
                    clear_run = []
            else:
                rise_run = []
        else:  # warning
            if r.water_level <= clear_threshold:
                clear_run.append(r)
                span = r.observed_at - clear_run[0].observed_at
                if len(clear_run) >= rules.clear_min_count and span >= rules.clear_min_span:
                    result.events.append(
                        Event(
                            type="cleared",
                            at=r.observed_at,
                            reading_id=r.id,
                            run_start=clear_run[0].observed_at,
                            run_count=len(clear_run),
                            span_seconds=span.total_seconds(),
                        )
                    )
                    status = "normal"
                    clear_run = []
                    rise_run = []
            else:
                clear_run = []

        prev = r

    if prev is not None:
        close_segment(prev)

    result.status = status
    if status == "warning":
        for e in reversed(result.events):
            if e.type == "warning":
                result.active_warning_since = e.at
                break
    return result
