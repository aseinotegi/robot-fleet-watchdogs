"""Alerts: one small interface, several channels."""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from enum import Enum
from typing import Protocol

log = logging.getLogger(__name__)


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return ("INFO", "WARNING", "CRITICAL").index(self.value)


@dataclass(frozen=True)
class Alert:
    severity: Severity
    robot: str
    title: str
    detail: str
    at: float
    source: str = ""

    def subject(self) -> str:
        return f"[{self.severity.value}] {self.robot}: {self.title}"

    def body(self) -> str:
        when = datetime.fromtimestamp(self.at, tz=timezone.utc).isoformat(timespec="seconds")
        return f"{self.title}\n\nRobot: {self.robot}\nWatchdog: {self.source}\nTime (UTC): {when}\n\n{self.detail}\n"


class Notifier(Protocol):
    def send(self, alert: Alert) -> None: ...


class LogNotifier:
    def send(self, alert: Alert) -> None:
        level = {Severity.INFO: logging.INFO, Severity.WARNING: logging.WARNING}.get(alert.severity, logging.ERROR)
        log.log(level, "ALERT %s — %s", alert.subject(), alert.detail)


@dataclass
class SmtpNotifier:
    host: str
    port: int
    sender: str
    recipients: list[str]
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    security: str = "starttls"  # starttls | ssl | none
    min_severity: Severity = Severity.WARNING
    timeout: float = 10.0

    def send(self, alert: Alert) -> None:
        if alert.severity.rank < Severity(self.min_severity).rank:
            return  # routine events stay in the log; e-mail is for what needs a human
        msg = EmailMessage()
        msg["Subject"] = alert.subject()
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients)
        msg.set_content(alert.body())
        context = ssl.create_default_context()
        if self.security == "ssl":
            server = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=context)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
        with server:
            if self.security == "starttls":
                server.starttls(context=context)
            if self.username:
                server.login(self.username, self.password or "")
            server.send_message(msg)


class MultiNotifier:
    """Sends to every channel; one failing channel never silences the others."""

    def __init__(self, notifiers: list[Notifier]) -> None:
        self._notifiers = notifiers
        self.sent: int = 0

    def send(self, alert: Alert) -> None:
        for notifier in self._notifiers:
            try:
                notifier.send(alert)
            except Exception as exc:  # noqa: BLE001 - a broken channel must not stop the watchdog
                log.error("notifier %s failed: %s", type(notifier).__name__, exc)
        self.sent += 1
