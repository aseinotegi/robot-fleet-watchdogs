import pytest

from fleet_watchdogs.graphql_platform import GraphQLPlatform
from fleet_watchdogs.platform import MissionState, PlatformError, Robot


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ValueError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))
        return self.responses.pop(0)


def platform(responses):
    session = FakeSession(responses)
    p = GraphQLPlatform("https://api/graphql", "https://api/login", "user", "key", [Robot("r1", "R1")], session=session)
    return p, session


LOGIN = Resp(200, {"token": "t1"})
MISSION = Resp(
    200,
    {
        "data": {
            "currentMission": {
                "id": "m1",
                "name": "Round",
                "status": "PAUSED",
                "lastEvent": {"type": "OBSTACLE_DETECTED", "timestamp": "2026-11-18T10:00:00Z"},
            }
        }
    },
)


def test_logs_in_once_and_parses_mission():
    p, session = platform([LOGIN, MISSION, MISSION])
    m = p.mission("r1")
    p.mission("r1")
    assert m.state == MissionState.PAUSED and m.last_event.kind == "OBSTACLE_DETECTED"
    assert m.last_event.at == 1794996000.0
    assert [c[0] for c in session.calls] == [
        "https://api/login",
        "https://api/graphql",
        "https://api/graphql",
    ]
    assert session.calls[1][2]["Authorization"] == "Bearer t1"
    assert session.calls[1][1]["variables"] == {"robotId": "r1"}


def test_refreshes_token_on_401_and_retries_once():
    p, session = platform([LOGIN, Resp(401, {}), Resp(200, {"token": "t2"}), MISSION])
    assert p.mission("r1").id == "m1"
    assert session.calls[-1][2]["Authorization"] == "Bearer t2"


def test_refreshes_token_on_graphql_unauthenticated_error():
    unauth = Resp(200, {"errors": [{"message": "expired", "extensions": {"code": "UNAUTHENTICATED"}}]})
    p, _ = platform([LOGIN, unauth, Resp(200, {"token": "t2"}), MISSION])
    assert p.mission("r1").id == "m1"


def test_graphql_errors_become_platform_errors():
    p, _ = platform([LOGIN, Resp(200, {"errors": [{"message": "robot offline"}]})])
    with pytest.raises(PlatformError, match="robot offline"):
        p.resume_mission("r1")


def test_failed_login_is_a_platform_error():
    p, _ = platform([Resp(403, {})])
    with pytest.raises(PlatformError, match="login failed"):
        p.battery("r1")


def test_sleep_and_wake_send_the_flag():
    ok = Resp(200, {"data": {"setRobotSleep": {"success": True}}})
    p, session = platform([LOGIN, ok, ok])
    p.sleep("r1")
    p.wake("r1")
    assert [c[1]["variables"]["asleep"] for c in session.calls[1:]] == [True, False]
