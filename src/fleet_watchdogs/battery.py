"""Battery: keep a history in SQLite and alert on low levels with hysteresis."""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from .notify import Alert, Notifier, Severity
from .platform import Robot, RobotPlatform

log = logging.getLogger(__name__)

OK, LOW, CRITICAL = "ok", "low", "critical"
_RANK = {OK: 0, LOW: 1, CRITICAL: 2}


@dataclass
class BatteryConfig:
    low: float = 20.0
    critical: float = 10.0
    clear: float = 25.0  # back to OK only above this: no flapping around the threshold
    record_every_s: float = 60.0
    retention_days: float = 30.0
    db_path: str = "state/battery.db"

    def __post_init__(self) -> None:
        if not self.critical < self.low < self.clear:
            raise ValueError("battery thresholds must satisfy critical < low < clear")


class BatteryStore:
    """SQLite history of battery readings.

    WAL mode lets the HTTP API (or any external reader) query the file while the
    watchdog keeps writing; inside the process one connection is shared under a lock.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._con = sqlite3.connect(path, timeout=30, check_same_thread=False)
        with self._lock, self._con:
            self._con.execute("PRAGMA journal_mode=WAL")
            self._con.execute(
                "CREATE TABLE IF NOT EXISTS readings ("
                " ts REAL NOT NULL, robot_id TEXT NOT NULL, percentage REAL NOT NULL,"
                " charging INTEGER NOT NULL, voltage REAL)"
            )
            self._con.execute("CREATE INDEX IF NOT EXISTS readings_robot_ts ON readings (robot_id, ts)")

    def record(self, ts: float, robot_id: str, percentage: float, charging: bool, voltage: float | None) -> None:
        with self._lock, self._con:
            self._con.execute(
                "INSERT INTO readings VALUES (?, ?, ?, ?, ?)",
                (ts, robot_id, percentage, int(charging), voltage),
            )

    def history(self, robot_id: str, since: float) -> list[dict]:
        with self._lock:
            rows = self._con.execute(
                "SELECT ts, percentage, charging, voltage FROM readings WHERE robot_id = ? AND ts >= ? ORDER BY ts",
                (robot_id, since),
            ).fetchall()
        return [{"ts": r[0], "percentage": r[1], "charging": bool(r[2]), "voltage": r[3]} for r in rows]

    def purge(self, older_than: float) -> int:
        with self._lock, self._con:
            return self._con.execute("DELETE FROM readings WHERE ts < ?", (older_than,)).rowcount

    def close(self) -> None:
        with self._lock:
            self._con.close()


class BatteryWatchdog:
    name = "battery"

    def __init__(
        self,
        platform: RobotPlatform,
        notifier: Notifier,
        config: BatteryConfig,
        store: BatteryStore | None = None,
    ) -> None:
        self._platform = platform
        self._notifier = notifier
        self._cfg = config
        self.store = store or BatteryStore(config.db_path)
        self._level: dict[str, str] = {}
        self._last_record: dict[str, float] = {}
        self._last_purge = 0.0
        self.latest: dict[str, dict] = {}

    def status(self) -> dict[str, dict]:
        return {rid: {**self.latest.get(rid, {}), "level": lvl} for rid, lvl in self._level.items()}

    def check(self, robot: Robot, now: float) -> None:
        battery = self._platform.battery(robot.id)
        self.latest[robot.id] = {"percentage": battery.percentage, "charging": battery.charging}

        if now - self._last_record.get(robot.id, float("-inf")) >= self._cfg.record_every_s:
            self.store.record(now, robot.id, battery.percentage, battery.charging, battery.voltage)
            self._last_record[robot.id] = now
        if now - self._last_purge >= 3600:
            removed = self.store.purge(now - self._cfg.retention_days * 86400)
            self._last_purge = now
            if removed:
                log.info("purged %d battery readings older than %.0f days", removed, self._cfg.retention_days)

        previous = self._level.get(robot.id, OK)
        level = self._classify(battery.percentage, previous)
        self._level[robot.id] = level
        if level == previous:
            return
        if _RANK[level] > _RANK[previous]:
            if battery.charging:
                return  # already being fixed: the level is tracked, but no alert
            severity = Severity.CRITICAL if level == CRITICAL else Severity.WARNING
            self._notifier.send(
                Alert(
                    severity,
                    robot.name,
                    f"Battery {level.upper()}: {battery.percentage:.0f}%",
                    "Send the robot to its dock or plan a manual charge.",
                    now,
                    self.name,
                )
            )
        elif level == OK:
            self._notifier.send(
                Alert(
                    Severity.INFO,
                    robot.name,
                    f"Battery recovered: {battery.percentage:.0f}%",
                    "",
                    now,
                    self.name,
                )
            )

    def _classify(self, pct: float, previous: str) -> str:
        if pct <= self._cfg.critical:
            return CRITICAL
        if pct <= self._cfg.low:
            return LOW
        if previous != OK and pct < self._cfg.clear:
            return LOW  # hysteresis band: stay in LOW until clearly recovered
        return OK
