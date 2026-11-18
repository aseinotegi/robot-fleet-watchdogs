import json
import urllib.request
from pathlib import Path

import pytest

from fleet_watchdogs.api import make_server, serve_in_background
from fleet_watchdogs.atex import AtexConfig, AtexWatchdog
from fleet_watchdogs.auto_resume import AutoResumeConfig, AutoResumeWatchdog
from fleet_watchdogs.battery import BatteryConfig, BatteryStore, BatteryWatchdog
from fleet_watchdogs.config import ConfigError, load_config, parse_config
from fleet_watchdogs.notify import Alert, MultiNotifier, Severity
from fleet_watchdogs.runner import Runner
from fleet_watchdogs.simulator import SimulatedFleet

from .fakes import Inbox

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example.yaml"


class Exploding:
    name = "exploding"

    def check(self, robot, now):
        if robot.id == "inspector-01":
            raise RuntimeError("boom")

    def status(self):
        return {}


class Counting:
    name = "counting"

    def __init__(self):
        self.seen = []

    def check(self, robot, now):
        self.seen.append(robot.id)

    def status(self):
        return {}


def test_one_failing_robot_does_not_stop_the_others():
    fleet = SimulatedFleet(size=3, seed=1)
    counting = Counting()
    runner = Runner(fleet, [Exploding(), counting])
    runner.tick()
    assert counting.seen == ["inspector-01", "inspector-02", "inspector-03"]
    assert runner.errors == {"exploding:inspector-01": 1}


def test_simulated_fleet_end_to_end():
    """Two simulated hours: the watchdogs must recover pauses and never crash."""
    clock = [1_700_000_000.0]
    fleet = SimulatedFleet(size=3, seed=7, pause_rate_s=60, clock=lambda: clock[0])
    inbox = Inbox()
    auto = AutoResumeWatchdog(fleet, inbox, AutoResumeConfig())
    watchdogs = [
        auto,
        AtexWatchdog(fleet, inbox, AtexConfig()),
        BatteryWatchdog(fleet, inbox, BatteryConfig(), BatteryStore(":memory:")),
    ]
    runner = Runner(fleet, watchdogs, clock=lambda: clock[0])
    for _ in range(2 * 3600 // 5):
        runner.tick()
        clock[0] += 5
    assert runner.errors == {}
    assert auto.recovered > 5
    assert any(a.title == "ATEX pressure LOW" for a in inbox.alerts)


def test_multi_notifier_survives_a_broken_channel():
    class Broken:
        def send(self, alert):
            raise OSError("smtp down")

    inbox = Inbox()
    multi = MultiNotifier([Broken(), inbox])
    multi.send(Alert(Severity.INFO, "r", "t", "d", 0.0))
    assert len(inbox.alerts) == 1


def test_example_config_loads():
    cfg = load_config(EXAMPLE)
    assert cfg.platform.type == "simulator"
    assert cfg.auto_resume.mode == "act" and "OBSTACLE_DETECTED" in cfg.auto_resume.recoverable_events
    assert cfg.battery.clear == 25


def test_disabled_watchdog_and_secrets(monkeypatch):
    monkeypatch.setenv("PLATFORM_API_KEY", "k")
    monkeypatch.setenv("PLATFORM_USERNAME", "u")
    cfg = parse_config(
        {
            "platform": {
                "type": "graphql",
                "graphql_url": "https://x/graphql",
                "login_url": "https://x/login",
            },
            "robots": [{"id": "a"}],
            "watchdogs": {"auto_resume": {"enabled": False}},
        }
    )
    assert cfg.auto_resume is None and cfg.platform.api_key == "k"
    assert "k" not in repr(cfg.platform)


@pytest.mark.parametrize(
    "raw",
    [
        {"platform": {"api_key": "plain"}},
        {"notifications": {"email": {"password": "plain"}}},
        {"platform": {"type": "graphql"}},
        {"platform": {"type": "other"}},
        {"watchdogs": {"battery": {"low": 5, "critical": 10}}},
        {"watchdogs": {"atex": {"unknown_key": 1}}},
        {"notifications": {"email": {"enabled": True}}},
    ],
)
def test_invalid_configs(raw, monkeypatch):
    monkeypatch.delenv("PLATFORM_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        parse_config(raw)


def test_http_api():
    store = BatteryStore(":memory:")
    store.record(9_999_999_999.0, "r1", 55.0, False, 48.1)
    server = make_server("127.0.0.1", 0, lambda: {"cycles": 3}, store)
    serve_in_background(server)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert json.load(urllib.request.urlopen(base + "/health")) == {"status": "ok"}
        assert json.load(urllib.request.urlopen(base + "/status")) == {"cycles": 3}
        body = json.load(urllib.request.urlopen(base + "/robots/r1/battery?hours=1"))
        assert body["readings"][0]["percentage"] == 55.0
    finally:
        server.shutdown()


def test_smtp_respects_min_severity(monkeypatch):
    from fleet_watchdogs.notify import SmtpNotifier

    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def send_message(self, msg):
            sent.append(msg["Subject"])

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    smtp = SmtpNotifier("h", 25, "a@x", ["b@x"], security="none", min_severity=Severity.WARNING)
    smtp.send(Alert(Severity.INFO, "R1", "resumed", "", 0.0))
    smtp.send(Alert(Severity.CRITICAL, "R1", "ATEX LOW", "", 0.0))
    assert sent == ["[CRITICAL] R1: ATEX LOW"]


def test_docker_config_loads():
    cfg = load_config(EXAMPLE.parent / "docker.yaml")
    assert cfg.email.enabled and cfg.email.host == "mailpit" and cfg.email.min_severity == "INFO"
