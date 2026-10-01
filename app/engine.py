"""预警/解除裁定引擎（纯函数，便于单测）。

输入：某站有效读数（已作废的由 SQL 过滤），按观测时间升序；上升阈值、解除阈值。
输出：预警时间线事件序列。

规则：
  1. 按观测时间重排有效读数；相邻读数间隔超过 15 分钟即断段。
  2. 同段内连续 >=3 个读数达到（>=）上升阈值，且首、尾相隔 >=10 分钟，
     在首次同时满足两项条件的读数处发出预警（rised）。
  3. 预警之后，连续 >=2 个读数不高于（<=）解除阈值，且首、尾相隔 >=5 分钟
     则解除（cleared）。间隔断段不自动解除，且会打断"连续"计数。
  4. 预警/解除严格交替；解除后重新按规则 2 评估。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Sequence


@dataclass
class ReadingPoint:
    id: int
    observed_at: float
    water_level: str


def compute_timeline(
    readings: Sequence[ReadingPoint],
    rise_threshold: str,
    clear_threshold: str,
    segment_gap: int,
    rise_min_readings: int,
    rise_min_span: int,
    clear_min_readings: int,
    clear_min_span: int,
) -> list[dict]:
    rise = Decimal(rise_threshold)
    clear = Decimal(clear_threshold)

    events: list[dict] = []
    # NORMAL: 未预警，累计同段高位读数；WARNED: 已预警，累计低位读数
    state = "NORMAL"
    run: list[ReadingPoint] = []

    def make_event(kind: str, first: ReadingPoint, trigger: ReadingPoint) -> dict:
        return {
            "event": kind,
            "first_reading_id": first.id,
            "trigger_reading_id": trigger.id,
            "first_observed_at": first.observed_at,
            "trigger_observed_at": trigger.observed_at,
            "reading_count": (rise_min_readings if kind == "rised" else clear_min_readings),
            "span_seconds": round(trigger.observed_at - first.observed_at, 6),
            "segment_gap_before": None,
        }

    prev: ReadingPoint | None = None
    for point in readings:
        gap = None if prev is None else round(point.observed_at - prev.observed_at, 6)
        segment_break = gap is not None and gap > segment_gap

        if state == "NORMAL":
            # 断段：正在累计的高位连续性作废
            if segment_break:
                run = []
            if Decimal(point.water_level) >= rise:
                run.append(point)
            else:
                run = []
            if (
                len(run) >= rise_min_readings
                and point.observed_at - run[0].observed_at >= rise_min_span
            ):
                events.append(make_event("rised", run[0], point))
                events[-1]["segment_gap_before"] = gap
                state = "WARNED"
                run = []
        else:  # WARNED
            # 断段不自动解除，但打断低位连续性
            if segment_break:
                run = []
            if Decimal(point.water_level) <= clear:
                run.append(point)
            else:
                run = []
            if (
                len(run) >= clear_min_readings
                and point.observed_at - run[0].observed_at >= clear_min_span
            ):
                events.append(make_event("cleared", run[0], point))
                events[-1]["segment_gap_before"] = gap
                state = "NORMAL"
                run = []
        prev = point

    return events


def current_state(events: Iterable[dict]) -> str:
    state = "normal"
    for event in events:
        state = "warning" if event["event"] == "rised" else "normal"
    return state
