from fleet_watchdogs.platform import Battery, Mission, MissionEvent, MissionState, PlatformError, Robot

ROBOT = Robot("r1", "Inspector 01")


class FakePlatform:
    """Scripted platform: tests set the state, actions are recorded."""

    def __init__(self):
        self.mission_value: Mission | None = Mission("m1", "Gauge round", MissionState.IN_PROGRESS)
        self.battery_value = Battery(80.0, False)
        self.atex_value = "PRESSURE_NOMINAL"
        self.actions: list[str] = []
        self.fail: dict[str, int] = {}
        self.resume_works = True
        self.wake_fixes = True
        self._woken = False

    def pause(self, kind: str, at: float, mission_id: str = "m1") -> None:
        self.mission_value = Mission(mission_id, "Gauge round", MissionState.PAUSED, MissionEvent(kind, at))

    def robots(self):
        return [ROBOT]

    def mission(self, robot_id):
        return self.mission_value

    def battery(self, robot_id):
        return self.battery_value

    def atex_pressure(self, robot_id):
        return self.atex_value

    def _act(self, name):
        if self.fail.get(name, 0) > 0:
            self.fail[name] -= 1
            self.actions.append(f"{name}!")
            raise PlatformError(f"{name} failed")
        self.actions.append(name)

    def clear_obstacle_map(self, robot_id):
        self._act("clear")

    def resume_mission(self, robot_id):
        self._act("resume")
        if self.resume_works or (self._woken and self.wake_fixes):
            m = self.mission_value
            self.mission_value = Mission(m.id, m.name, MissionState.IN_PROGRESS)

    def sleep(self, robot_id):
        self._act("sleep")

    def wake(self, robot_id):
        self._act("wake")
        self._woken = True


class Inbox:
    def __init__(self):
        self.alerts = []

    def send(self, alert):
        self.alerts.append(alert)

    def titles(self):
        return [a.title for a in self.alerts]
