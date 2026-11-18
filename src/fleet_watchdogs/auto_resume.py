"""Auto-resume: put paused missions back to work, but only when it is safe.

One small state machine per robot, advanced on every check. Nothing blocks or sleeps,
so a single loop can supervise the whole fleet and one slow robot never delays others.

    PAUSED (recoverable, recent event)
      │ clear obstacle map (with retries)
      ▼
    SETTLING ──► resume ──► VERIFYING ──► running again ✓
                                │ still paused
                                ▼
    fallback: sleep ─► WAKING ─► wake ─► RESUMING ─► resume ─► VERIFYING ──► running ✓
                                                                   │ still paused
                                                                   ▼
                                                     ESCALATED: operator needed
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from .notify import Alert, Notifier, Severity
from .platform import Mission, MissionState, PlatformError, Robot, RobotPlatform

log = logging.getLogger(__name__)

DEFAULT_RECOVERABLE = ("OBSTACLE_DETECTED", "DOCKING_TIMEOUT", "NAVIGATION_FAILED", "LOCALIZATION_LOST")


class Phase(str, Enum):
    IDLE = "idle"
    SETTLING = "settling"
    VERIFYING = "verifying"
    WAKING = "waking"
    RESUMING = "resuming"
    ESCALATED = "escalated"


@dataclass
class AutoResumeConfig:
    mode: str = "act"  # "notify": only report what it would do · "act": recover
    recoverable_events: tuple[str, ...] = DEFAULT_RECOVERABLE
    max_event_age_s: float = 120.0
    clear_retries: int = 3
    settle_s: float = 5.0
    verify_s: float = 20.0

    def __post_init__(self) -> None:
        if self.mode not in ("notify", "act"):
            raise ValueError("auto_resume.mode must be 'notify' or 'act'")
        self.recoverable_events = tuple(e.upper() for e in self.recoverable_events)


@dataclass
class Recovery:
    pause_key: tuple | None = None
    phase: Phase = Phase.IDLE
    next_at: float = 0.0
    started_at: float = 0.0
    used_fallback: bool = False
    steps: list[str] = field(default_factory=list)


class AutoResumeWatchdog:
    name = "auto-resume"

    def __init__(self, platform: RobotPlatform, notifier: Notifier, config: AutoResumeConfig) -> None:
        self._platform = platform
        self._notifier = notifier
        self._cfg = config
        self._robots: dict[str, Recovery] = {}
        self.recovered = 0
        self.escalated = 0

    def status(self) -> dict[str, str]:
        return {robot_id: rec.phase.value for robot_id, rec in self._robots.items()}

    def check(self, robot: Robot, now: float) -> None:
        mission = self._platform.mission(robot.id)
        rec = self._robots.setdefault(robot.id, Recovery())

        if mission is None or mission.state != MissionState.PAUSED:
            if rec.phase in (Phase.VERIFYING, Phase.SETTLING, Phase.WAKING, Phase.RESUMING) and mission:
                if mission.state == MissionState.IN_PROGRESS:
                    self._succeeded(robot, mission, rec, now)
            self._robots[robot.id] = Recovery()
            return

        key = self._pause_key(mission)
        if rec.pause_key != key:
            rec = self._robots[robot.id] = Recovery(pause_key=key, started_at=now)
            self._on_new_pause(robot, mission, rec, now)
            return

        if rec.phase in (Phase.IDLE, Phase.ESCALATED) or now < rec.next_at:
            return
        self._advance(robot, mission, rec, now)

    # -- decisions --------------------------------------------------------------

    def _on_new_pause(self, robot: Robot, mission: Mission, rec: Recovery, now: float) -> None:
        event = mission.last_event
        kind = event.kind.upper() if event else "UNKNOWN"
        if kind not in self._cfg.recoverable_events:
            self._escalate(
                robot,
                rec,
                now,
                Severity.WARNING,
                f"Mission '{mission.name}' paused: {kind}",
                "This cause is not on the recoverable list. An operator must check the robot.",
            )
            return
        age = now - event.at
        if age > self._cfg.max_event_age_s:
            self._escalate(
                robot,
                rec,
                now,
                Severity.WARNING,
                f"Mission '{mission.name}' paused: {kind}",
                f"The event is {age:.0f} s old (limit {self._cfg.max_event_age_s:.0f} s); "
                "conditions may have changed, so it will not be resumed automatically.",
            )
            return
        if self._cfg.mode == "notify":
            self._escalate(
                robot,
                rec,
                now,
                Severity.INFO,
                f"Mission '{mission.name}' paused: {kind}",
                "Notify-only mode: in 'act' mode this pause would be recovered automatically.",
            )
            return
        log.info("[%s] %s paused by %s; starting recovery", robot.name, mission.name, kind)
        self._clear_and_settle(robot, rec, now)

    def _advance(self, robot: Robot, mission: Mission, rec: Recovery, now: float) -> None:
        if rec.phase == Phase.SETTLING or rec.phase == Phase.RESUMING:
            self._do(robot, rec, "resume", self._platform.resume_mission)
            rec.phase, rec.next_at = Phase.VERIFYING, now + self._cfg.verify_s
        elif rec.phase == Phase.VERIFYING:
            if rec.used_fallback:
                self._escalate(
                    robot,
                    rec,
                    now,
                    Severity.CRITICAL,
                    f"Automatic recovery failed: '{mission.name}'",
                    "Tried: " + " → ".join(rec.steps) + ". The mission is still paused.",
                )
                return
            log.info("[%s] still paused after resume; trying sleep/wake fallback", robot.name)
            rec.used_fallback = True
            self._do(robot, rec, "sleep", self._platform.sleep)
            rec.phase, rec.next_at = Phase.WAKING, now + self._cfg.settle_s
        elif rec.phase == Phase.WAKING:
            self._do(robot, rec, "wake", self._platform.wake)
            rec.phase, rec.next_at = Phase.RESUMING, now + self._cfg.settle_s

    def _clear_and_settle(self, robot: Robot, rec: Recovery, now: float) -> None:
        for attempt in range(1, self._cfg.clear_retries + 1):
            try:
                self._platform.clear_obstacle_map(robot.id)
                rec.steps.append("clear obstacle map")
                break
            except PlatformError as exc:
                log.warning(
                    "[%s] clear obstacle map failed (%d/%d): %s",
                    robot.name,
                    attempt,
                    self._cfg.clear_retries,
                    exc,
                )
        else:
            rec.steps.append("clear obstacle map ✗")
        rec.phase, rec.next_at = Phase.SETTLING, now + self._cfg.settle_s

    def _do(self, robot: Robot, rec: Recovery, step: str, action) -> None:
        try:
            action(robot.id)
            rec.steps.append(step)
        except PlatformError as exc:
            rec.steps.append(f"{step} ✗")
            log.warning("[%s] %s failed: %s", robot.name, step, exc)

    def _succeeded(self, robot: Robot, mission: Mission, rec: Recovery, now: float) -> None:
        self.recovered += 1
        self._notifier.send(
            Alert(
                Severity.INFO,
                robot.name,
                f"Mission '{mission.name}' resumed automatically",
                f"Recovered in {now - rec.started_at:.0f} s. Steps: " + " → ".join(rec.steps),
                now,
                self.name,
            )
        )

    def _escalate(self, robot: Robot, rec: Recovery, now: float, severity: Severity, title: str, detail: str) -> None:
        rec.phase = Phase.ESCALATED
        if severity != Severity.INFO:
            self.escalated += 1
        self._notifier.send(Alert(severity, robot.name, title, detail, now, self.name))

    @staticmethod
    def _pause_key(mission: Mission) -> tuple:
        event = mission.last_event
        return (mission.id, event.kind if event else None, event.at if event else None)
