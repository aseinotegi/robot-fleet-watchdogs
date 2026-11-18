"""In-memory robot fleet that misbehaves on purpose, for demos and tests.

Missions pause now and then for different reasons. Some recover with a simple resume,
some need the obstacle map cleared, some only with a sleep/wake cycle, and an
emergency stop never recovers by itself. Batteries drain on mission and charge on the
dock; ATEX pressure has single-reading glitches and real drops.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .platform import Battery, Mission, MissionEvent, MissionState, PlatformError, Robot

PAUSE_CAUSES = (
    ("OBSTACLE_DETECTED", 0.45),
    ("DOCKING_TIMEOUT", 0.2),
    ("NAVIGATION_FAILED", 0.2),
    ("EMERGENCY_STOP", 0.15),
)
MISSIONS = ("Daily gauge round", "Gas detection loop", "Thermal inspection")


@dataclass
class _SimRobot:
    robot: Robot
    mission_no: int = 0
    state: MissionState = MissionState.IN_PROGRESS
    event: MissionEvent | None = None
    needs_clear: bool = False
    needs_wake: bool = False
    cleared: bool = False
    asleep: bool = False
    battery: float = 80.0
    charging: bool = False
    atex: str = "PRESSURE_NOMINAL"
    atex_low_until: float = 0.0
    last: float = 0.0


class SimulatedFleet:
    def __init__(
        self,
        size: int = 3,
        seed: int | None = None,
        pause_rate_s: float = 45.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._rng = random.Random(seed)
        self._clock = clock
        self._pause_rate = pause_rate_s
        self._lock = threading.Lock()
        now = clock()
        self._robots = {
            f"inspector-{i:02d}": _SimRobot(
                Robot(f"inspector-{i:02d}", f"Inspector {i:02d}"), battery=self._rng.uniform(40, 95), last=now
            )
            for i in range(1, size + 1)
        }

    # -- RobotPlatform ------------------------------------------------------------

    def robots(self) -> list[Robot]:
        return [r.robot for r in self._robots.values()]

    def mission(self, robot_id: str) -> Mission | None:
        r = self._tick(robot_id)
        name = MISSIONS[r.mission_no % len(MISSIONS)]
        return Mission(f"{robot_id}-m{r.mission_no}", name, r.state, r.event)

    def battery(self, robot_id: str) -> Battery:
        r = self._tick(robot_id)
        return Battery(round(r.battery, 1), r.charging, round(44 + r.battery * 0.08, 2))

    def atex_pressure(self, robot_id: str) -> str:
        r = self._tick(robot_id)
        if r.atex == "PRESSURE_NOMINAL" and self._rng.random() < 0.01:
            return "PRESSURE_LOW"  # isolated glitch: must not raise an alert
        return r.atex

    def clear_obstacle_map(self, robot_id: str) -> None:
        r = self._get(robot_id)
        if self._rng.random() < 0.1:
            raise PlatformError("timeout clearing obstacle map")
        r.cleared = True

    def resume_mission(self, robot_id: str) -> None:
        r = self._get(robot_id)
        if r.state != MissionState.PAUSED or r.asleep:
            return
        if r.event and r.event.kind == "EMERGENCY_STOP":
            return
        if (r.needs_clear and not r.cleared) or r.needs_wake:
            return  # stays paused: the robot was not really fixed
        r.state, r.event = MissionState.IN_PROGRESS, None

    def sleep(self, robot_id: str) -> None:
        self._get(robot_id).asleep = True

    def wake(self, robot_id: str) -> None:
        r = self._get(robot_id)
        r.asleep, r.needs_wake, r.needs_clear = False, False, False

    # -- simulation -----------------------------------------------------------------

    def _get(self, robot_id: str) -> _SimRobot:
        try:
            return self._robots[robot_id]
        except KeyError:
            raise PlatformError(f"unknown robot {robot_id}") from None

    def _tick(self, robot_id: str) -> _SimRobot:
        with self._lock:
            r = self._get(robot_id)
            now = self._clock()
            dt, r.last = max(now - r.last, 0.0), now
            if r.state == MissionState.IN_PROGRESS:
                r.battery -= 0.04 * dt
                if self._rng.random() < dt / self._pause_rate:
                    self._pause(r, now)
                elif self._rng.random() < dt / 300:
                    r.state = MissionState.COMPLETED
            elif r.state == MissionState.COMPLETED:
                r.charging = True
                if r.battery > 90 or self._rng.random() < dt / 120:
                    r.charging, r.mission_no = False, r.mission_no + 1
                    r.state = MissionState.IN_PROGRESS
            elif r.state == MissionState.PAUSED and r.event and r.event.kind == "EMERGENCY_STOP":
                if now - r.event.at > 90:  # an operator resets the e-stop after a while
                    r.state, r.event = MissionState.IN_PROGRESS, None
            if r.charging:
                r.battery += 0.3 * dt
            r.battery = min(max(r.battery, 3.0), 100.0)
            if r.atex == "PRESSURE_LOW" and now >= r.atex_low_until:
                r.atex = "PRESSURE_NOMINAL"
            elif r.atex == "PRESSURE_NOMINAL" and self._rng.random() < dt / 240:
                r.atex, r.atex_low_until = "PRESSURE_LOW", now + 30
            return r

    def _pause(self, r: _SimRobot, now: float) -> None:
        kinds, weights = zip(*PAUSE_CAUSES, strict=True)
        kind = self._rng.choices(kinds, weights)[0]
        r.state, r.event = MissionState.PAUSED, MissionEvent(kind, now)
        r.cleared = False
        r.needs_clear = kind == "OBSTACLE_DETECTED"
        r.needs_wake = kind == "NAVIGATION_FAILED" and self._rng.random() < 0.5
