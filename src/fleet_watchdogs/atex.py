"""ATEX enclosure pressure: alert on confirmed state changes, never on every reading."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .notify import Alert, Notifier, Severity
from .platform import Robot, RobotPlatform

log = logging.getLogger(__name__)

NOMINAL, LOW = "NOMINAL", "LOW"


@dataclass
class AtexConfig:
    confirmation_readings: int = 2
    nominal_states: tuple[str, ...] = ("PRESSURE_NOMINAL",)
    low_states: tuple[str, ...] = ("PRESSURE_LOW", "PRESSURE_CRITICALLY_LOW")
    state_file: str | None = None

    def __post_init__(self) -> None:
        if self.confirmation_readings < 1:
            raise ValueError("atex.confirmation_readings must be >= 1")


class StateConfirmer:
    """A new state only counts after N consecutive identical readings.

    That includes the very first state after start-up: one isolated bad reading (a sensor
    glitch, a dropped packet) never triggers an alert.
    """

    def __init__(self, required: int, stable: str | None = None) -> None:
        self.required = required
        self.stable = stable
        self._candidate: str | None = None
        self._count = 0

    def observe(self, state: str) -> tuple[str | None, str] | None:
        """Return ``(previous, new)`` when a change is confirmed."""
        if state == self.stable:
            self._candidate, self._count = None, 0
            return None
        if state == self._candidate:
            self._count += 1
        else:
            self._candidate, self._count = state, 1
        if self._count >= self.required:
            previous, self.stable = self.stable, state
            self._candidate, self._count = None, 0
            return previous, state
        return None


class AtexWatchdog:
    name = "atex-pressure"

    def __init__(self, platform: RobotPlatform, notifier: Notifier, config: AtexConfig) -> None:
        self._platform = platform
        self._notifier = notifier
        self._cfg = config
        self._nominal = {s.upper() for s in config.nominal_states}
        self._low = {s.upper() for s in config.low_states}
        saved = self._load()
        self._confirmers: dict[str, StateConfirmer] = {
            robot_id: StateConfirmer(config.confirmation_readings, state) for robot_id, state in saved.items()
        }

    def status(self) -> dict[str, str | None]:
        return {robot_id: c.stable for robot_id, c in self._confirmers.items()}

    def classify(self, raw: str | None) -> str | None:
        value = (raw or "").strip().upper()
        if value in self._nominal:
            return NOMINAL
        if value in self._low:
            return LOW
        return None

    def check(self, robot: Robot, now: float) -> None:
        raw = self._platform.atex_pressure(robot.id)
        state = self.classify(raw)
        if state is None:
            log.debug("[%s] ignoring unknown ATEX state %r", robot.name, raw)
            return
        confirmer = self._confirmers.setdefault(robot.id, StateConfirmer(self._cfg.confirmation_readings))
        change = confirmer.observe(state)
        if change is None:
            return
        previous, current = change
        self._save()
        if previous is None:
            if current == LOW:  # starting in a low state is itself worth an alert
                self._alert(robot, now, Severity.CRITICAL, "ATEX pressure LOW at start-up", raw)
            return
        if current == LOW:
            self._alert(
                robot,
                now,
                Severity.CRITICAL,
                "ATEX pressure LOW",
                f"Confirmed after {self._cfg.confirmation_readings} readings (raw: {raw}). "
                "The robot must not operate in a hazardous area until it is restored.",
            )
        else:
            self._alert(robot, now, Severity.INFO, "ATEX pressure back to nominal", f"Raw: {raw}")

    def _alert(self, robot: Robot, now: float, severity: Severity, title: str, detail: str) -> None:
        self._notifier.send(Alert(severity, robot.name, title, detail, now, self.name))

    # -- persistence: a restart must not re-alert on a state already reported --

    def _load(self) -> dict[str, str]:
        path = self._cfg.state_file
        if not path or not Path(path).is_file():
            return {}
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("cannot read ATEX state file (%s); starting fresh", exc)
            return {}
        return {k: v for k, v in data.items() if v in (NOMINAL, LOW)}

    def _save(self) -> None:
        path = self._cfg.state_file
        if not path:
            return
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # write-then-rename: the file is never left half written
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".atex-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.status(), fh)
        os.replace(tmp, target)
