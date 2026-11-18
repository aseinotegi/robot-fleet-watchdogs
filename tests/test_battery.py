import pytest

from fleet_watchdogs.battery import BatteryConfig, BatteryStore, BatteryWatchdog
from fleet_watchdogs.notify import Severity
from fleet_watchdogs.platform import Battery

from .fakes import ROBOT, FakePlatform, Inbox


@pytest.fixture
def env():
    platform, inbox = FakePlatform(), Inbox()
    store = BatteryStore(":memory:")
    wd = BatteryWatchdog(platform, inbox, BatteryConfig(record_every_s=60), store)
    return wd, platform, inbox, store


def levels(wd, platform, values, charging=False, t0=0.0):
    for i, pct in enumerate(values):
        platform.battery_value = Battery(pct, charging)
        wd.check(ROBOT, t0 + i)


def test_alerts_on_each_worsening_level(env):
    wd, platform, inbox, _ = env
    levels(wd, platform, [50, 30, 19, 15, 9])
    assert [a.severity for a in inbox.alerts] == [Severity.WARNING, Severity.CRITICAL]


def test_hysteresis_avoids_flapping_around_the_threshold(env):
    wd, platform, inbox, _ = env
    levels(wd, platform, [21, 19, 21, 19, 22, 19, 24])
    assert len(inbox.alerts) == 1  # one LOW alert, no OK/LOW ping-pong
    levels(wd, platform, [26], t0=100)
    assert inbox.titles()[-1].startswith("Battery recovered")


def test_no_alert_while_charging(env):
    wd, platform, inbox, _ = env
    levels(wd, platform, [15, 12], charging=True)
    assert inbox.alerts == []
    assert wd.status()["r1"]["level"] == "low"


def test_readings_are_recorded_at_the_configured_rate(env):
    wd, platform, _, store = env
    for t in range(0, 300, 5):
        platform.battery_value = Battery(80 - t / 10, False, 48.0)
        wd.check(ROBOT, 1000.0 + t)
    history = store.history("r1", since=0)
    assert len(history) == 5  # every 60 s over 300 s
    assert history[0] == {"ts": 1000.0, "percentage": 80.0, "charging": False, "voltage": 48.0}


def test_store_purge(tmp_path):
    store = BatteryStore(str(tmp_path / "b.db"))
    store.record(10, "r1", 50, False, None)
    store.record(20, "r1", 49, False, None)
    assert store.purge(older_than=15) == 1
    assert [r["ts"] for r in store.history("r1", 0)] == [20]
    store.close()


def test_thresholds_must_be_ordered():
    with pytest.raises(ValueError):
        BatteryConfig(low=10, critical=20)
