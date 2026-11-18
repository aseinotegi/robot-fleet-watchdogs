import pytest

from fleet_watchdogs.auto_resume import AutoResumeConfig, AutoResumeWatchdog, Phase
from fleet_watchdogs.notify import Severity

from .fakes import ROBOT, FakePlatform, Inbox

T0 = 1_700_000_000.0


@pytest.fixture
def env():
    platform, inbox = FakePlatform(), Inbox()
    cfg = AutoResumeConfig(settle_s=5, verify_s=20, max_event_age_s=120, clear_retries=3)
    return AutoResumeWatchdog(platform, inbox, cfg), platform, inbox


def run(wd, start, seconds, step=1.0):
    t = start
    while t <= start + seconds:
        wd.check(ROBOT, t)
        t += step
    return t


def test_running_mission_is_left_alone(env):
    wd, platform, inbox = env
    run(wd, T0, 60)
    assert platform.actions == [] and inbox.alerts == []


def test_recoverable_pause_is_cleared_then_resumed_and_reported(env):
    wd, platform, inbox = env
    platform.pause("OBSTACLE_DETECTED", at=T0 - 3)
    wd.check(ROBOT, T0)
    assert platform.actions == ["clear"]  # resume waits for the settle time
    wd.check(ROBOT, T0 + 4)
    assert platform.actions == ["clear"]
    run(wd, T0 + 5, 30)
    assert platform.actions == ["clear", "resume"]
    assert inbox.titles() == ["Mission 'Gauge round' resumed automatically"]
    assert inbox.alerts[0].severity == Severity.INFO
    assert wd.recovered == 1


def test_non_recoverable_cause_is_escalated_once_without_touching_the_robot(env):
    wd, platform, inbox = env
    platform.pause("EMERGENCY_STOP", at=T0)
    run(wd, T0, 120)
    assert platform.actions == []
    assert len(inbox.alerts) == 1 and "EMERGENCY_STOP" in inbox.alerts[0].title
    assert wd.status()["r1"] == Phase.ESCALATED.value


def test_old_event_is_not_resumed(env):
    wd, platform, inbox = env
    platform.pause("OBSTACLE_DETECTED", at=T0 - 600)
    run(wd, T0, 30)
    assert platform.actions == []
    assert "600 s old" in inbox.alerts[0].detail


def test_notify_mode_reports_but_never_acts():
    platform, inbox = FakePlatform(), Inbox()
    wd = AutoResumeWatchdog(platform, inbox, AutoResumeConfig(mode="notify"))
    platform.pause("OBSTACLE_DETECTED", at=T0)
    run(wd, T0, 60)
    assert platform.actions == []
    assert "Notify-only mode" in inbox.alerts[0].detail


def test_clear_is_retried_when_the_platform_fails(env):
    wd, platform, _ = env
    platform.fail["clear"] = 2
    platform.pause("OBSTACLE_DETECTED", at=T0)
    wd.check(ROBOT, T0)
    assert platform.actions == ["clear!", "clear!", "clear"]


def test_fallback_sleep_wake_when_resume_does_not_work(env):
    wd, platform, inbox = env
    platform.resume_works = False
    platform.pause("NAVIGATION_FAILED", at=T0)
    run(wd, T0, 80)
    assert platform.actions == ["clear", "resume", "sleep", "wake", "resume"]
    assert inbox.titles() == ["Mission 'Gauge round' resumed automatically"]
    assert "sleep → wake → resume" in inbox.alerts[0].detail


def test_escalates_when_every_strategy_fails(env):
    wd, platform, inbox = env
    platform.resume_works = False
    platform.wake_fixes = False
    platform.pause("DOCKING_TIMEOUT", at=T0)
    run(wd, T0, 200)
    assert platform.actions == ["clear", "resume", "sleep", "wake", "resume"]  # no endless loop
    assert len(inbox.alerts) == 1
    assert inbox.alerts[0].severity == Severity.CRITICAL
    assert wd.escalated == 1


def test_operator_resuming_during_recovery_ends_it(env):
    wd, platform, inbox = env
    platform.pause("OBSTACLE_DETECTED", at=T0)
    wd.check(ROBOT, T0)
    platform.resume_mission("r1")  # someone else resumed it
    run(wd, T0 + 1, 40)
    assert wd.status()["r1"] == Phase.IDLE.value


def test_a_new_pause_starts_a_new_recovery(env):
    wd, platform, inbox = env
    platform.pause("OBSTACLE_DETECTED", at=T0)
    t = run(wd, T0, 40)
    platform.pause("OBSTACLE_DETECTED", at=t)
    run(wd, t, 40)
    assert platform.actions == ["clear", "resume", "clear", "resume"]
    assert wd.recovered == 2


def test_invalid_mode():
    with pytest.raises(ValueError):
        AutoResumeConfig(mode="yolo")
