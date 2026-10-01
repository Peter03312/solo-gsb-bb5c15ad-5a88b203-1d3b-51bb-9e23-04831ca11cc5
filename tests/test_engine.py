"""裁定引擎规则单测。"""
from __future__ import annotations

from app.engine import ReadingPoint, compute_timeline, current_state

GAP = 900


def tl(points, rise="5", clear="4"):
    return compute_timeline(
        points,
        rise_threshold=rise,
        clear_threshold=clear,
        segment_gap=GAP,
        rise_min_readings=3,
        rise_min_span=600,
        clear_min_readings=2,
        clear_min_span=300,
    )


def pt(i, minute, level):
    return ReadingPoint(id=i, observed_at=minute * 60, water_level=str(level))


def test_warning_requires_three_readings_and_ten_minutes():
    # 3 个高位但跨度仅 8 分钟，不触发；第 4 个高位（跨度 12 分钟）触发
    points = [pt(1, 0, 5.1), pt(2, 4, 5.2), pt(3, 8, 5.3), pt(4, 12, 5.4)]
    events = tl(points)
    assert len(events) == 1
    assert events[0]["event"] == "rised"
    assert events[0]["trigger_reading_id"] == 4
    assert events[0]["first_reading_id"] == 1


def test_low_reading_breaks_high_run():
    points = [pt(1, 0, 5.5), pt(2, 5, 3.0), pt(3, 10, 5.5), pt(4, 15, 5.6), pt(5, 20, 5.7)]
    events = tl(points)
    # 最后三个高位从第 3 个起算，跨度 10 分钟 -> 触发
    assert [e["event"] for e in events] == ["rised"]
    assert events[0]["first_reading_id"] == 3


def test_segment_break_breaks_rise_run():
    # 两个高位 -> 断段（20 分钟）-> 一个高位，不构成同段 3 个
    points = [pt(1, 0, 6.0), pt(2, 5, 6.0), pt(3, 25, 6.0)]
    assert tl(points) == []


def test_clear_requires_two_lows_and_five_minutes():
    points = [
        pt(1, 0, 5.5), pt(2, 5, 5.5), pt(3, 10, 5.5),   # 预警
        pt(4, 15, 4.0),                                  # 低位 1
        pt(5, 18, 4.0),                                  # 低位 2 但跨度仅 3 分钟
        pt(6, 20, 4.0),                                  # 跨度 5 分钟 -> 解除
    ]
    events = tl(points)
    assert [e["event"] for e in events] == ["rised", "cleared"]
    assert events[1]["first_reading_id"] == 4
    assert events[1]["trigger_reading_id"] == 6
    assert current_state(events) == "normal"


def test_high_reading_breaks_clear_run():
    points = [
        pt(1, 0, 6), pt(2, 5, 6), pt(3, 10, 6),
        pt(4, 15, 3), pt(5, 20, 5), pt(6, 25, 3), pt(7, 30, 3),
    ]
    events = tl(points)
    assert [e["event"] for e in events] == ["rised", "cleared"]


def test_segment_break_does_not_auto_clear_but_breaks_low_run():
    points = [
        pt(1, 0, 6), pt(2, 5, 6), pt(3, 10, 6),   # 预警
        pt(4, 15, 3),                               # 低位 1
        pt(5, 40, 3),                               # 断段 25 分钟：不自动解除，计数清零
        pt(6, 45, 3),                               # 从这里起重新累计 2 个、跨度 5 分钟
    ]
    events = tl(points)
    assert [e["event"] for e in events] == ["rised", "cleared"]
    assert events[1]["first_reading_id"] == 5
    assert events[1]["trigger_reading_id"] == 6


def test_threshold_is_inclusive():
    points = [pt(1, 0, 5), pt(2, 5, 5), pt(3, 10, 5)]  # 恰等于上升阈值
    events = tl(points)
    assert [e["event"] for e in events] == ["rised"]


def test_can_warn_again_after_clearing():
    points = [
        pt(1, 0, 6), pt(2, 5, 6), pt(3, 10, 6),       # 预警
        pt(4, 15, 3), pt(5, 20, 3),                    # 解除
        pt(6, 25, 6), pt(7, 30, 6), pt(8, 35, 6),     # 再次预警（跨度 10 分钟）
    ]
    events = tl(points)
    assert [e["event"] for e in events] == ["rised", "cleared", "rised"]
