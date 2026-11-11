"""End-to-end flow against fake site/captcha backends (no network)."""

import json
import threading
import time
import urllib.request
from datetime import datetime, timedelta

from booker import TZ, Booker, Plan
from captcha_desk import CaptchaDesk
from client import ApiError, Captcha
from config import Venue

_VENUE = Venue("Main hall", "scene", "devkind", "classtype")
from fakes import FULL, NOT_OPEN, site


# ------------------------------------------------------------- captcha desk
class FakeCaptchaBackend:
    """Accepts x == 100 only (stands in for the site's check endpoint)."""

    def __init__(self):
        self.issued = 0

    def get_captcha(self):
        self.issued += 1
        return Captcha(token=f"t{self.issued}", secret_key="k" * 16, background_b64="AAAA", piece_b64="BBBB")

    def check_captcha(self, captcha, x):
        return f"verified:{captcha.token}:{x}" if x == 100 else None


def _get(url):
    return json.loads(urllib.request.urlopen(url, timeout=5).read())


def _answer(desk, cid, x):
    req = urllib.request.Request(desk.url + "/answer", data=json.dumps({"id": cid, "x": x}).encode(), method="POST")
    urllib.request.urlopen(req, timeout=5).read()


def _wait_for_challenge(desk, not_id=None):
    for _ in range(100):
        ch = _get(desk.url + "/state")["challenge"]
        if ch and ch["id"] != not_id:
            return ch
        time.sleep(0.05)
    raise AssertionError("no challenge appeared")


def test_desk_serves_page_and_retries_until_human_gets_it_right():
    backend = FakeCaptchaBackend()
    desk = CaptchaDesk(backend, open_browser=False)
    desk.start()
    try:
        page = urllib.request.urlopen(desk.url, timeout=5).read().decode()
        assert "Drag" in page or "drag" in page
        result = {}
        t = threading.Thread(target=lambda: result.setdefault("v", desk.solve(deadline=time.time() + 10)))
        t.start()

        first = _wait_for_challenge(desk)
        assert first["bg"] == "AAAA" and first["piece"] == "BBBB"
        _answer(desk, first["id"], 30)               # wrong -> new challenge
        second = _wait_for_challenge(desk, not_id=first["id"])
        assert second["prompt"].startswith("Not quite")
        _answer(desk, "stale-id", 100)              # answers for old challenges are ignored
        _answer(desk, second["id"], 100.4)          # right (rounded)
        t.join(5)
        assert result["v"] == "verified:t2:100"
        assert _get(desk.url + "/state")["challenge"] is None
    finally:
        desk.stop()


def test_desk_rejects_requests_without_the_url_key():
    desk = CaptchaDesk(FakeCaptchaBackend(), open_browser=False)
    desk.start()
    try:
        base = desk.url.rsplit("/", 1)[0]
        try:
            urllib.request.urlopen(base + "/state", timeout=5)
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        desk.stop()


# ------------------------------------------------------------------ booker
class FakeClient:
    def __init__(self, sites_before, sites_after, fail_first_with=None):
        self.sites_before, self.sites_after = sites_before, sites_after
        self.fail_first_with = fail_first_with
        self.opened = False
        self.reserve_bodies = []
        self.auth = None

    def login_user(self):
        return {"id": "user-42", "name": "Tester"}

    def server_time_offset(self):
        return 0.0

    def list_sites(self, venue, date):
        return self.sites_after if self.opened else self.sites_before

    def workflow_deploy_uuid(self, form_uuid):
        return "deploy-1"

    def add_reserve(self, body):
        self.reserve_bodies.append(body)
        if self.fail_first_with and len(self.reserve_bodies) == 1:
            raise ApiError(self.fail_first_with)
        return {"resvIds": ["resv-1"], "periodResvConflict": []}

    def order_check(self, resv_ids, user_id):
        return {"orderGenerated": True}

    def get_order(self, resv_uuid):
        return {"orderNo": "N1", "payableAmount": 3000, "orderStatus": "TO_BE_PAID", "payDeadline": "later"}

    def refresh_token(self):
        return True


class FakeDesk:
    url = "http://fake"

    def __init__(self):
        self.solved = 0

    def solve(self, deadline=None, reason=""):
        self.solved += 1
        return f"captcha-{self.solved}"

    def log(self, line):
        pass

    def set_headline(self, *a, **k):
        pass


def _plan(**kw):
    base = dict(venue=_VENUE, date="2026-11-03", times=[("19:00", None), ("20:00", None)], courts=[5, 6])
    base.update(kw)
    return Plan(**base)


def test_book_now_falls_back_to_next_court_when_first_is_taken():
    sites = [site(5, {}), site(6, {})]
    client = FakeClient(sites, sites, fail_first_with="该时段已被预约")
    client.opened = True
    desk = FakeDesk()
    result = Booker(client, desk, _plan()).run()

    assert result and result.slot.court == "羽06" and result.slot.begin == "19:00"
    assert result.order["orderNo"] == "N1"
    first, second = client.reserve_bodies
    assert first["siteUuid"] == "site-5" and second["siteUuid"] == "site-6"
    # Same captcha reused: the error wasn't about the captcha.
    assert first["captcha"] == second["captcha"] == "captcha-1"
    # Body has the exact shape the browser sends.
    assert second == {
        "sceneUuid": "scene-1", "sceneUseType": "SPORT_GROUP", "siteUuid": "site-6", "siteType": "DEV",
        "reserveTime": [{"startTime": "2026-11-03 19:00:00", "endTime": "2026-11-03 20:00:00"}],
        "siteSessionReserve": [{"sessionDetailUuid": "sess-6-19:00",
                                "reserveTime": {"startTime": "2026-11-03 19:00:00", "endTime": "2026-11-03 20:00:00"}}],
        "resvMember": ["user-42"], "resvKind": "CURRENT_RESERVE", "payType": "PAY_ONLINE", "purchaseUuid": "",
        "formParam": {"formId": "form-1", "deployUuid": "deploy-1", "variables": {}, "chooseCandidates": {}},
        "captcha": "captcha-1",
    }


def test_captcha_error_asks_for_a_new_captcha():
    sites = [site(5, {})]
    client = FakeClient(sites, sites, fail_first_with="验证码校验失败")
    client.opened = True
    desk = FakeDesk()
    result = Booker(client, desk, _plan(courts=[5])).run()
    assert result and desk.solved == 2
    assert [b["captcha"] for b in client.reserve_bodies] == ["captcha-1", "captcha-2"]
    assert all(b["siteUuid"] == "site-5" for b in client.reserve_bodies)  # same slot retried


def test_book_now_with_nothing_free_returns_none():
    sites = [site(5, {"19:00": FULL, "20:00": FULL})]
    client = FakeClient(sites, sites)
    client.opened = True
    assert Booker(client, FakeDesk(), _plan()).run() is None
    assert client.reserve_bodies == []


def test_scheduled_run_presolves_captcha_and_fires_after_opening():
    before = [site(5, {"19:00": NOT_OPEN, "20:00": NOT_OPEN}), site(6, {"19:00": NOT_OPEN, "20:00": NOT_OPEN})]
    after = [site(5, {"19:00": FULL}), site(6, {})]
    client = FakeClient(before, after, fail_first_with="当前场次预约人数已满")
    desk = FakeDesk()
    fire_at = datetime.now(TZ) + timedelta(seconds=1.0)
    threading.Timer(0.9, lambda: setattr(client, "opened", True)).start()

    result = Booker(client, desk, _plan(fire_at=fire_at, captcha_lead=0.5, fire_delay=0.05, give_up_after=10)).run()

    assert result and (result.slot.court, result.slot.begin) == ("羽06", "19:00")
    # First attempt fired from the pre-loaded list, not before opening.
    assert len(client.reserve_bodies) == 2
    assert client.reserve_bodies[0]["siteUuid"] == "site-5"
    assert time.time() >= fire_at.timestamp()


def test_scheduled_run_when_new_day_lists_nothing_until_opening():
    # What the real site does: the new day returns no courts at all before 08:00.
    after = [site(5, {}), site(6, {})]
    client = FakeClient([], after)
    desk = FakeDesk()
    fire_at = datetime.now(TZ) + timedelta(seconds=1.0)
    # Opens 0.1 s *after* our first list request goes out: the staggered duplicates must cover it.
    threading.Timer(1.2, lambda: setattr(client, "opened", True)).start()

    result = Booker(client, desk, _plan(fire_at=fire_at, captcha_lead=0.5, fire_delay=0.1, give_up_after=10)).run()

    assert result and (result.slot.court, result.slot.begin) == ("羽05", "19:00")
    assert len(client.reserve_bodies) == 1
    assert client.reserve_bodies[0]["formParam"]["deployUuid"] == "deploy-1"


def test_timeout_that_actually_booked_is_detected_not_double_booked():
    import requests

    sites_free = [site(5, {}), site(6, {})]
    taken_by_me = [dict(site(5, {"19:00": FULL}), reserveInfo=[{"startTime": "19:00", "userId": "user-42"}]),
                   site(6, {})]

    class TimeoutClient(FakeClient):
        def add_reserve(self, body):
            self.reserve_bodies.append(body)
            self.sites_after = taken_by_me          # server booked it…
            raise requests.ReadTimeout("slow")      # …but the answer never arrived

    client = TimeoutClient(sites_free, sites_free)
    client.opened = True
    result = Booker(client, FakeDesk(), _plan()).run()
    assert result and result.slot.court == "羽05"
    assert len(client.reserve_bodies) == 1           # did not go on to book 羽06 as well


def test_dry_run_never_calls_add_reserve():
    sites = [site(5, {})]
    client = FakeClient(sites, sites)
    client.opened = True
    result = Booker(client, None, _plan(dry_run=True)).run()
    assert result and result.slot.court == "羽05"
    assert client.reserve_bodies == []
