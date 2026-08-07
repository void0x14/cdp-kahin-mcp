"""Humanized input generator tests (pure Python)."""

from __future__ import annotations

from kahin.humanize import bezier_trajectory, jittered_delay, typing_cadence


def test_trajectory_endpoints_and_length() -> None:
    points = bezier_trajectory(10, 20, 310, 220, steps=24, jitter=0.0)
    assert len(points) == 24
    assert points[0] == (10.0, 20.0)
    assert points[-1] == (310.0, 220.0)


def test_trajectory_progresses_toward_target() -> None:
    points = bezier_trajectory(0, 0, 1000, 0, steps=10, jitter=0.0)
    xs = [p[0] for p in points]
    assert xs == sorted(xs)
    assert xs[-1] == 1000.0


def test_trajectory_jitter_stays_bounded() -> None:
    points = bezier_trajectory(0, 0, 100, 100, steps=40, jitter=2.0, seed=7)
    for x, y in points:
        assert -2.0 <= x <= 102.0
        assert -2.0 <= y <= 102.0


def test_trajectory_deterministic_with_seed() -> None:
    a = bezier_trajectory(5, 5, 200, 90, steps=20, jitter=3.0, seed=42)
    b = bezier_trajectory(5, 5, 200, 90, steps=20, jitter=3.0, seed=42)
    assert a == b


def test_jittered_delay_range_and_determinism() -> None:
    for _ in range(200):
        delay = jittered_delay(50.0, 10.0, seed=1)
        assert 40.0 <= delay <= 60.0
    assert jittered_delay(50.0, 10.0, seed=9) == jittered_delay(50.0, 10.0, seed=9)


def test_typing_cadence_length_and_range() -> None:
    delays = typing_cadence(5, base_ms=45.0, jitter_ms=25.0, seed=3)
    assert len(delays) == 5
    for delay in delays:
        assert 20.0 <= delay <= 70.0
