"""Vendor-neutral view of a robot fleet platform.

Watchdogs only talk to this interface. Adapters translate it to a concrete platform
(GraphQL API, simulator, …), so the decision logic never depends on one vendor.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class MissionState(str, Enum):
    IN_PROGRESS = "IN_PROGRESS"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class Robot:
    id: str
    name: str


@dataclass(frozen=True)
class MissionEvent:
    kind: str  # e.g. OBSTACLE_DETECTED, DOCKING_TIMEOUT, EMERGENCY_STOP
    at: float  # epoch seconds


@dataclass(frozen=True)
class Mission:
    id: str
    name: str
    state: MissionState
    last_event: MissionEvent | None = None


@dataclass(frozen=True)
class Battery:
    percentage: float
    charging: bool
    voltage: float | None = None


class RobotPlatform(Protocol):
    def robots(self) -> list[Robot]: ...

    def mission(self, robot_id: str) -> Mission | None: ...

    def battery(self, robot_id: str) -> Battery: ...

    def atex_pressure(self, robot_id: str) -> str:
        """Raw pressure state reported by the robot (e.g. PRESSURE_NOMINAL)."""
        ...

    def clear_obstacle_map(self, robot_id: str) -> None: ...

    def resume_mission(self, robot_id: str) -> None: ...

    def sleep(self, robot_id: str) -> None: ...

    def wake(self, robot_id: str) -> None: ...


class PlatformError(RuntimeError):
    """The platform rejected a request or could not be reached."""
