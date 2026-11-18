from fleet_watchdogs.atex import LOW, NOMINAL, AtexConfig, AtexWatchdog, StateConfirmer
from fleet_watchdogs.notify import Severity

from .fakes import ROBOT, FakePlatform, Inbox


def feed(wd, platform, readings, t0=0.0):
    for i, raw in enumerate(readings):
        platform.atex_value = raw
        wd.check(ROBOT, t0 + i)


def test_confirmer_needs_consecutive_readings():
    c = StateConfirmer(required=2)
    assert c.observe(NOMINAL) is None
    assert c.observe(NOMINAL) == (None, NOMINAL)  # the initial state is confirmed too
    assert c.observe(LOW) is None
    assert c.observe(NOMINAL) is None  # glitch discarded
    assert c.observe(LOW) is None
    assert c.observe(LOW) == (NOMINAL, LOW)


def test_isolated_glitch_does_not_alert():
    platform, inbox = FakePlatform(), Inbox()
    wd = AtexWatchdog(platform, inbox, AtexConfig(confirmation_readings=2))
    feed(wd, platform, ["PRESSURE_NOMINAL", "PRESSURE_NOMINAL", "PRESSURE_LOW", "PRESSURE_NOMINAL", "PRESSURE_NOMINAL"])
    assert inbox.alerts == []


def test_confirmed_drop_and_recovery_alert_once_each():
    platform, inbox = FakePlatform(), Inbox()
    wd = AtexWatchdog(platform, inbox, AtexConfig(confirmation_readings=2))
    feed(wd, platform, ["PRESSURE_NOMINAL"] * 2 + ["PRESSURE_CRITICALLY_LOW"] * 5 + ["PRESSURE_NOMINAL"] * 5)
    assert inbox.titles() == ["ATEX pressure LOW", "ATEX pressure back to nominal"]
    assert inbox.alerts[0].severity == Severity.CRITICAL


def test_low_at_start_up_is_reported():
    platform, inbox = FakePlatform(), Inbox()
    wd = AtexWatchdog(platform, inbox, AtexConfig())
    feed(wd, platform, ["PRESSURE_LOW", "PRESSURE_LOW"])
    assert inbox.titles() == ["ATEX pressure LOW at start-up"]


def test_glitch_on_the_very_first_reading_is_ignored():
    platform, inbox = FakePlatform(), Inbox()
    wd = AtexWatchdog(platform, inbox, AtexConfig())
    feed(wd, platform, ["PRESSURE_LOW", "PRESSURE_NOMINAL", "PRESSURE_NOMINAL", "PRESSURE_NOMINAL"])
    assert inbox.alerts == [] and wd.status() == {"r1": NOMINAL}


def test_unknown_states_are_ignored():
    platform, inbox = FakePlatform(), Inbox()
    wd = AtexWatchdog(platform, inbox, AtexConfig())
    feed(wd, platform, ["PRESSURE_NOMINAL", "SENSOR_ERROR", None, "PRESSURE_NOMINAL"])
    assert inbox.alerts == [] and wd.status() == {"r1": NOMINAL}


def test_state_survives_restart_without_repeating_alerts(tmp_path):
    state = tmp_path / "atex.json"
    platform, inbox = FakePlatform(), Inbox()
    wd = AtexWatchdog(platform, inbox, AtexConfig(state_file=str(state)))
    feed(wd, platform, ["PRESSURE_NOMINAL", "PRESSURE_NOMINAL", "PRESSURE_LOW", "PRESSURE_LOW"])
    assert inbox.titles() == ["ATEX pressure LOW"]

    restarted_inbox = Inbox()
    restarted = AtexWatchdog(platform, restarted_inbox, AtexConfig(state_file=str(state)))
    feed(restarted, platform, ["PRESSURE_LOW"] * 3)
    assert restarted_inbox.alerts == []  # already reported before the restart


def test_corrupt_state_file_starts_fresh(tmp_path):
    state = tmp_path / "atex.json"
    state.write_text("{not json")
    wd = AtexWatchdog(FakePlatform(), Inbox(), AtexConfig(state_file=str(state)))
    assert wd.status() == {}
