"""Supervision loop: every watchdog checks every robot, failures stay isolated."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Protocol

from .platform import Robot, RobotPlatform

log = logging.getLogger(__name__)


class Watchdog(Protocol):
    name: str

    def check(self, robot: Robot, now: float) -> None: ...

    def status(self) -> dict: ...


class Runner:
    def __init__(
        self,
        platform: RobotPlatform,
        watchdogs: list[Watchdog],
        interval_s: float = 5.0,
        robots_refresh_s: float = 300.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._platform = platform
        self.watchdogs = watchdogs
        self._interval = interval_s
        self._robots_refresh = robots_refresh_s
        self._clock = clock
        self._robots: list[Robot] = []
        self._robots_at = float("-inf")
        self.errors: dict[str, int] = {}
        self.cycles = 0

    def tick(self) -> None:
        now = self._clock()
        if now - self._robots_at >= self._robots_refresh or not self._robots:
            try:
                self._robots, self._robots_at = self._platform.robots(), now
            except Exception as exc:  # noqa: BLE001 - keep the last known list
                log.warning("cannot refresh robot list: %s", exc)
        for robot in self._robots:
            for watchdog in self.watchdogs:
                try:
                    watchdog.check(robot, now)
                except Exception as exc:  # noqa: BLE001 - one robot or watchdog never stops the rest
                    key = f"{watchdog.name}:{robot.id}"
                    self.errors[key] = self.errors.get(key, 0) + 1
                    log.warning("[%s] %s check failed: %s", robot.name, watchdog.name, exc)
        self.cycles += 1

    def run(self, stop: threading.Event) -> None:
        log.info("supervising with %s every %.0f s", ", ".join(w.name for w in self.watchdogs), self._interval)
        while not stop.is_set():
            started = time.monotonic()
            self.tick()
            stop.wait(max(self._interval - (time.monotonic() - started), 0.0))

    def status(self) -> dict:
        return {
            "cycles": self.cycles,
            "robots": [r.id for r in self._robots],
            "errors": self.errors,
            "watchdogs": {w.name: w.status() for w in self.watchdogs},
        }
