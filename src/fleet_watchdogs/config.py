"""YAML for behaviour, environment variables for credentials."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

from .atex import AtexConfig
from .auto_resume import AutoResumeConfig
from .battery import BatteryConfig
from .platform import Robot


class ConfigError(ValueError):
    pass


@dataclass
class PlatformConfig:
    type: str = "simulator"  # simulator | graphql
    graphql_url: str | None = None
    login_url: str | None = None
    simulated_robots: int = 3
    username: str | None = None
    api_key: str | None = field(default=None, repr=False)


@dataclass
class EmailConfig:
    enabled: bool = False
    host: str = "localhost"
    port: int = 587
    security: str = "starttls"
    min_severity: str = "WARNING"
    sender: str = "fleet-watchdogs@localhost"
    recipients: list[str] = field(default_factory=list)
    username: str | None = None
    password: str | None = field(default=None, repr=False)


@dataclass
class ApiConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass
class Config:
    platform: PlatformConfig
    robots: list[Robot]
    check_interval_s: float
    auto_resume: AutoResumeConfig | None
    atex: AtexConfig | None
    battery: BatteryConfig | None
    email: EmailConfig
    api: ApiConfig


def _make(cls, raw, section: str):
    raw = dict(raw or {})
    allowed = {f.name for f in fields(cls)}
    if "enabled" not in allowed:
        raw.pop("enabled", None)
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"unknown keys in '{section}': {', '.join(sorted(unknown))}")
    for key, value in raw.items():
        if isinstance(value, list) and key != "recipients":
            raw[key] = tuple(value)
    try:
        return cls(**raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid '{section}': {exc}") from None


def _watchdog(cls, raw: dict, name: str):
    section = raw.get(name)
    if section is None or not section.get("enabled", True):
        return None
    return _make(cls, section, f"watchdogs.{name}")


def parse_config(raw: dict) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("the configuration must be a YAML mapping")
    if "api_key" in (raw.get("platform") or {}):
        raise ConfigError("do not store 'api_key' in the YAML file; use PLATFORM_API_KEY")
    if "password" in ((raw.get("notifications") or {}).get("email") or {}):
        raise ConfigError("do not store 'password' in the YAML file; use SMTP_PASSWORD")

    platform = _make(PlatformConfig, raw.get("platform"), "platform")
    env = os.environ
    platform.username = env.get("PLATFORM_USERNAME", platform.username)
    platform.api_key = env.get("PLATFORM_API_KEY")
    if platform.type not in ("simulator", "graphql"):
        raise ConfigError("platform.type must be 'simulator' or 'graphql'")

    robots = [Robot(str(r["id"]), str(r.get("name", r["id"]))) for r in raw.get("robots") or []]
    if platform.type == "graphql":
        missing = [k for k in ("graphql_url", "login_url", "username", "api_key") if not getattr(platform, k)]
        if missing:
            raise ConfigError(f"graphql platform needs: {', '.join(missing)} (credentials via PLATFORM_* env vars)")
        if not robots:
            raise ConfigError("list the robots to supervise under 'robots'")

    watchdogs = raw.get("watchdogs") or {}
    email = _make(EmailConfig, (raw.get("notifications") or {}).get("email"), "notifications.email")
    email.username = env.get("SMTP_USERNAME", email.username)
    email.password = env.get("SMTP_PASSWORD")
    if email.min_severity not in ("INFO", "WARNING", "CRITICAL"):
        raise ConfigError("notifications.email.min_severity must be INFO, WARNING or CRITICAL")
    if email.enabled and not email.recipients:
        raise ConfigError("notifications.email needs at least one recipient")

    return Config(
        platform=platform,
        robots=robots,
        check_interval_s=float(raw.get("check_interval_s", 5)),
        auto_resume=_watchdog(AutoResumeConfig, watchdogs, "auto_resume"),
        atex=_watchdog(AtexConfig, watchdogs, "atex"),
        battery=_watchdog(BatteryConfig, watchdogs, "battery"),
        email=email,
        api=_make(ApiConfig, raw.get("api"), "api"),
    )


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"configuration file not found: {path}")
    with path.open(encoding="utf-8") as fh:
        return parse_config(yaml.safe_load(fh))
