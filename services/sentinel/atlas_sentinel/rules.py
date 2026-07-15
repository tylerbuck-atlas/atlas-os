# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Sentinel's anomaly rules (v1).

Rules consume verified bus events and raise alerts. Deliberately simple
and legible — an unauditable security monitor is itself a risk:

- **service.down** — a service transitioned to unhealthy/unreachable.
- **service.flapping** — ≥ N status transitions for one service inside a
  sliding window (crash loops, fights over a name).
- **policy.rejection** — the Planner refused a plan. One rejection is a
  mistake; a stream of them is something probing the policy fence.

Alerts are deduplicated per (kind, subject) inside a cooldown so a
crash-looping service raises one alarm, not a siren per heartbeat.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field


@dataclass
class AlertCandidate:
    kind: str
    subject: str
    severity: str  # info | warning | critical
    detail: str


@dataclass
class RuleEngine:
    flap_threshold: int = 4
    flap_window_seconds: float = 60.0
    rejection_threshold: int = 3
    rejection_window_seconds: float = 300.0
    cooldown_seconds: float = 60.0

    _transitions: dict[str, deque] = field(default_factory=lambda: defaultdict(deque))
    _rejections: dict[str, deque] = field(default_factory=lambda: defaultdict(deque))
    _last_raised: dict[tuple[str, str], float] = field(default_factory=dict)

    def _dedup(self, candidate: AlertCandidate, now: float) -> AlertCandidate | None:
        key = (candidate.kind, candidate.subject)
        last = self._last_raised.get(key)
        if last is not None and now - last < self.cooldown_seconds:
            return None
        self._last_raised[key] = now
        return candidate

    @staticmethod
    def _trim(window: deque, now: float, span: float) -> None:
        while window and now - window[0] > span:
            window.popleft()

    def evaluate(self, event: dict, *, now: float | None = None) -> list[AlertCandidate]:
        """Feed one bus event envelope; get zero or more alerts."""
        now = time.monotonic() if now is None else now
        topic = event.get("topic", "")
        payload = event.get("payload", {})
        alerts: list[AlertCandidate] = []

        if topic == "registry.service.status_changed":
            name = payload.get("name", "?")
            to = payload.get("to")

            window = self._transitions[name]
            window.append(now)
            self._trim(window, now, self.flap_window_seconds)

            if to in ("unhealthy", "unreachable"):
                alerts.append(AlertCandidate(
                    kind="service.down",
                    subject=name,
                    severity="warning" if to == "unhealthy" else "critical",
                    detail=f"{name} is {to}: {payload.get('reason', 'unknown')}",
                ))
            if len(window) >= self.flap_threshold:
                alerts.append(AlertCandidate(
                    kind="service.flapping",
                    subject=name,
                    severity="warning",
                    detail=(
                        f"{name}: {len(window)} status transitions in "
                        f"{self.flap_window_seconds:.0f}s"
                    ),
                ))

        elif topic == "planner.plan.rejected":
            requester = payload.get("requester", "?")
            window = self._rejections[requester]
            window.append(now)
            self._trim(window, now, self.rejection_window_seconds)

            alerts.append(AlertCandidate(
                kind="policy.rejection",
                subject=requester,
                severity="info",
                detail=(
                    f"plan by {requester} rejected: "
                    f"{payload.get('reason', 'policy denial')}"
                ),
            ))
            if len(window) >= self.rejection_threshold:
                alerts.append(AlertCandidate(
                    kind="policy.probing",
                    subject=requester,
                    severity="critical",
                    detail=(
                        f"{requester}: {len(window)} policy rejections in "
                        f"{self.rejection_window_seconds:.0f}s — possible probing"
                    ),
                ))

        return [a for a in (self._dedup(a, now) for a in alerts) if a is not None]
