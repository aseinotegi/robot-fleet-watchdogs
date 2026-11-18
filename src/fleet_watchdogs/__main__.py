"""Command line entry point: ``fleet-watchdogs -c config.yaml``."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from . import __version__
from .api import make_server, serve_in_background
from .atex import AtexWatchdog
from .auto_resume import AutoResumeWatchdog
from .battery import BatteryWatchdog
from .config import Config, ConfigError, load_config
from .graphql_platform import GraphQLPlatform
from .notify import LogNotifier, MultiNotifier, Notifier, Severity, SmtpNotifier
from .runner import Runner
from .simulator import SimulatedFleet


def build_platform(cfg: Config):
    p = cfg.platform
    if p.type == "simulator":
        return SimulatedFleet(size=p.simulated_robots)
    return GraphQLPlatform(p.graphql_url, p.login_url, p.username, p.api_key, cfg.robots)


def build_notifier(cfg: Config) -> MultiNotifier:
    channels: list[Notifier] = [LogNotifier()]
    e = cfg.email
    if e.enabled:
        channels.append(
            SmtpNotifier(
                e.host,
                e.port,
                e.sender,
                list(e.recipients),
                e.username,
                e.password,
                e.security,
                Severity(e.min_severity),
            )
        )
    return MultiNotifier(channels)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fleet-watchdogs", description="Autonomous watchdogs for robot fleets")
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        logging.error("configuration error: %s", exc)
        return 2

    platform, notifier = build_platform(cfg), build_notifier(cfg)
    watchdogs = []
    if cfg.auto_resume:
        watchdogs.append(AutoResumeWatchdog(platform, notifier, cfg.auto_resume))
    if cfg.atex:
        watchdogs.append(AtexWatchdog(platform, notifier, cfg.atex))
    battery = BatteryWatchdog(platform, notifier, cfg.battery) if cfg.battery else None
    if battery:
        watchdogs.append(battery)
    if not watchdogs:
        logging.error("no watchdog enabled")
        return 2

    runner = Runner(platform, watchdogs, cfg.check_interval_s)
    server = None
    if cfg.api.enabled:
        server = make_server(cfg.api.host, cfg.api.port, runner.status, battery.store if battery else None)
        serve_in_background(server)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        runner.run(stop)
    finally:
        if server:
            server.shutdown()
        logging.info("stopped after %d cycles, %d alerts sent", runner.cycles, notifier.sent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
