"""humanize.py — Human-like input generation (deterministic with seed).

A quadratic Bézier with a random control point plus bounded Gaussian
jitter produces mouse paths that look human: curved, accelerating at the
ends, never perfectly straight, never a single teleport. Typing cadence
returns per-character delays with realistic jitter. Everything is seed-
able so tests and replays are reproducible.

Every generator is bounded: trajectory steps are capped at 200, jitter at
20px (truncated to ±jitter so output never leaves the declared range),
delays are clamped to their base-relative range, and cadence length is
capped. No generator can emit unbounded output.
"""

from __future__ import annotations

import math
import random

_DEFAULT_STEPS = 24
_DEFAULT_JITTER = 2.0
_JITTER_MAX = 20.0
_STEPS_MAX = 200
_CADENCE_MAX_LENGTH = 10_000


def bezier_trajectory(
    x0: float, y0: float, x1: float, y1: float, *,
    steps: int = _DEFAULT_STEPS, jitter: float = _DEFAULT_JITTER,
    seed: int | None = None,
) -> list[tuple[float, float]]:
    """Points from (x0,y0) to (x1,y1) along a jittered quadratic Bézier.

    The control point is the midpoint pushed along the perpendicular by a
    bounded random bend, so the path curves one way or the other but stays
    directional. Jitter is a truncated Gaussian: sampled normally around
    zero but clamped to ±jitter, keeping every point inside
    [min - jitter, max + jitter] of the endpoints.
    """
    steps = max(2, min(int(steps), _STEPS_MAX))
    jitter = max(0.0, min(float(jitter), _JITTER_MAX))
    rng = random.Random(seed)
    # control point: biased toward the midpoint with perpendicular offset
    mid_x, mid_y = (x0 + x1) / 2, (y0 + y1) / 2
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy) or 1.0
    perp_x, perp_y = -dy / length, dx / length
    bend = rng.uniform(-0.35, 0.35) * length
    control = (mid_x + perp_x * bend, mid_y + perp_y * bend)
    points: list[tuple[float, float]] = []
    for step in range(steps):
        t = step / (steps - 1)
        inv = 1.0 - t
        x = inv * inv * x0 + 2 * inv * t * control[0] + t * t * x1
        y = inv * inv * y0 + 2 * inv * t * control[1] + t * t * y1
        if 0 < t < 1 and jitter > 0:
            x += max(-jitter, min(jitter, rng.gauss(0.0, jitter)))
            y += max(-jitter, min(jitter, rng.gauss(0.0, jitter)))
        points.append((round(x, 2), round(y, 2)))
    points[0] = (float(x0), float(y0))
    points[-1] = (float(x1), float(y1))
    return points


def jittered_delay(base_ms: float, jitter_ms: float, seed: int | None = None) -> float:
    """base ± jitter, clamped to [0.25*base, 4*base]."""
    base = max(0.0, float(base_ms))
    jitter = max(0.0, float(jitter_ms))
    rng = random.Random(seed)
    delay = base + rng.uniform(-jitter, jitter)
    return round(max(base * 0.25, min(delay, base * 4.0)), 1)


def typing_cadence(
    length: int, base_ms: float = 45.0, jitter_ms: float = 25.0,
    seed: int | None = None,
) -> list[float]:
    """Per-character delays (ms) for typing ``length`` characters.

    Most delays sit at base ± jitter; every 4-7 characters a slight
    slowdown is attempted, clamped so the total never exceeds base +
    jitter — the cadence range stays bounded and consistent.
    """
    length = max(0, min(int(length), _CADENCE_MAX_LENGTH))
    base = max(0.0, float(base_ms))
    jitter = max(0.0, float(jitter_ms))
    rng = random.Random(seed)
    delays: list[float] = []
    for index in range(length):
        # slight slowdown after every 4-7 chars mimics hand pauses
        burst = rng.randint(4, 7)
        extra = rng.uniform(0.0, 90.0) if index > 0 and index % burst == 0 else 0.0
        delay = base + rng.uniform(-jitter, jitter) + extra
        delays.append(round(min(max(delay, 5.0), base + jitter), 1))
    return delays
