"""Adapter for a robot fleet platform that exposes a GraphQL API with token login.

Queries live in ``queries/*.graphql`` and follow a generic schema; adapt them (and
``_parse_*``) to your vendor. Everything else (watchdogs, alerts, storage) stays the same.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from importlib import resources

import requests

from .platform import Battery, Mission, MissionEvent, MissionState, PlatformError, Robot

log = logging.getLogger(__name__)


def _load_query(name: str) -> str:
    return resources.files("fleet_watchdogs.queries").joinpath(f"{name}.graphql").read_text(encoding="utf-8")


def _epoch(value: str | float | None) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class GraphQLPlatform:
    """Logs in, caches the token and refreshes it before it expires or when the API says so."""

    def __init__(
        self,
        url: str,
        login_url: str,
        username: str,
        api_key: str,
        robots: list[Robot],
        session: requests.Session | None = None,
        token_ttl_s: float = 3000.0,
        timeout_s: float = 10.0,
    ) -> None:
        self._url, self._login_url = url, login_url
        self._username, self._api_key = username, api_key
        self._robots = robots
        self._session = session or requests.Session()
        self._ttl, self._timeout = token_ttl_s, timeout_s
        self._token: str | None = None
        self._token_expires = 0.0
        self._queries: dict[str, str] = {}

    # -- RobotPlatform ------------------------------------------------------------

    def robots(self) -> list[Robot]:
        return list(self._robots)

    def mission(self, robot_id: str) -> Mission | None:
        data = self._query("mission", {"robotId": robot_id}).get("currentMission")
        if not data:
            return None
        event = data.get("lastEvent")
        return Mission(
            id=data["id"],
            name=data.get("name") or data["id"],
            state=MissionState(data["status"]),
            last_event=MissionEvent(event["type"], _epoch(event.get("timestamp"))) if event else None,
        )

    def battery(self, robot_id: str) -> Battery:
        data = self._query("battery", {"robotId": robot_id})["robotStatus"]["battery"]
        return Battery(float(data["percentage"]), data.get("chargingState") == "CHARGING", data.get("voltage"))

    def atex_pressure(self, robot_id: str) -> str:
        return self._query("atex_pressure", {"robotId": robot_id})["robotStatus"]["atexPressure"]["state"]

    def clear_obstacle_map(self, robot_id: str) -> None:
        self._query("clear_obstacle_map", {"robotId": robot_id})

    def resume_mission(self, robot_id: str) -> None:
        self._query("resume_mission", {"robotId": robot_id})

    def sleep(self, robot_id: str) -> None:
        self._query("set_sleep", {"robotId": robot_id, "asleep": True})

    def wake(self, robot_id: str) -> None:
        self._query("set_sleep", {"robotId": robot_id, "asleep": False})

    # -- transport ------------------------------------------------------------------

    def _token_value(self, force: bool = False) -> str:
        if force or not self._token or time.time() >= self._token_expires:
            try:
                resp = self._session.post(
                    self._login_url,
                    timeout=self._timeout,
                    json={"username": self._username, "apiKey": self._api_key},
                )
                resp.raise_for_status()
                self._token = resp.json()["token"]
            except (requests.RequestException, KeyError, ValueError) as exc:
                raise PlatformError(f"login failed: {exc}") from None
            self._token_expires = time.time() + self._ttl
            log.info("platform token refreshed")
        return self._token

    def _query(self, name: str, variables: dict) -> dict:
        query = self._queries.setdefault(name, _load_query(name))
        for attempt in (1, 2):
            headers = {"Authorization": f"Bearer {self._token_value(force=attempt == 2)}"}
            try:
                resp = self._session.post(
                    self._url,
                    json={"query": query, "variables": variables},
                    headers=headers,
                    timeout=self._timeout,
                )
            except requests.RequestException as exc:
                raise PlatformError(f"{name}: {exc}") from None
            if resp.status_code == 401 and attempt == 1:
                continue  # expired early: log in again and retry once
            if resp.status_code >= 400:
                raise PlatformError(f"{name}: HTTP {resp.status_code}")
            body = resp.json()
            errors = body.get("errors") or []
            if any((e.get("extensions") or {}).get("code") == "UNAUTHENTICATED" for e in errors) and attempt == 1:
                continue
            if errors:
                raise PlatformError(f"{name}: {errors[0].get('message', errors[0])}")
            return body.get("data") or {}
        raise PlatformError(f"{name}: not authorised after refreshing the token")
