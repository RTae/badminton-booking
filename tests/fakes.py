"""Synthetic API data shaped like the real court-list response (no real people)."""

FREE = {"reserveStatus": "Y", "reserveStatusReason": "", "code": 0}
FULL = {"reserveStatus": "N", "reserveStatusReason": "当前场次预约人数已满", "code": 401000136}
NOT_OPEN = {"reserveStatus": "N", "reserveStatusReason": "未开放", "code": 40100060}

SESSIONS = [("18:00", "19:00"), ("19:00", "20:00"), ("20:00", "22:00")]


def site(n: int, statuses: dict[str, dict]) -> dict:
    """A court ``羽NN`` whose sessions have the given status by begin time (default free)."""
    return {
        "uuid": f"site-{n}",
        "sceneUuid": "scene-1",
        "siteName": f"羽{n:02d}",
        "formRuleVo": {"formUuid": "form-1"},
        "sessionVo": [
            {"uuid": f"sess-{n}-{b}", "beginTime": b, "endTime": e,
             "reserveStatus": statuses.get(b, FREE)}
            for b, e in SESSIONS
        ],
    }
