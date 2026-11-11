"""Overnight mode: start at midnight, log in at 07:30, alarm for the captcha, book at 08:00.

Time is compressed: "07:30" is ~0.6 s from now and "08:00" ~1.6 s.
"""

import base64
import json
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import booker as booker_mod
import cli
from auth import Auth
from booker import TZ, Booker, Plan, _Alarm
from client import AuthError
from config import Venue

_VENUE = Venue("Main hall", "scene", "devkind", "classtype")
from fakes import site
from test_flow import FakeClient, FakeDesk


def jwt(exp: float) -> str:
    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'none'})}.{b64({'exp': int(exp), 'username': 'u'})}.sig"


class LoginClient(FakeClient):
    """FakeClient with a real-looking token whose renewal we can watch."""

    def __init__(self, auth, relogin_ok=True, renew_results=None):
        sites = [site(5, {}), site(6, {})]
        super().__init__(sites, sites)
        self.opened = True
        self.auth = auth
        self.events = []
        self.relogin_ok = relogin_ok
        self.renew_results = list(renew_results or [])
        self.relogin = self._relogin

    def _relogin(self):
        self.events.append(("relogin", time.time()))
        return Auth(jwt(time.time() + 5 * 3600), jwt(time.time() + 2 * 3600)) if self.relogin_ok else None

    def _set_auth(self, a):
        self.auth = a

    def renew_login(self):
        ok = self.renew_results.pop(0) if self.renew_results else True
        self.events.append(("renew", time.time(), ok))
        if ok:
            self.auth = Auth(jwt(time.time() + 5 * 3600), jwt(time.time() + 2 * 3600))
        return ok

    def login_user(self):
        self.events.append(("login_user", time.time()))
        return super().login_user()


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(booker_mod, "PREP_MIN", 0.3)
    monkeypatch.setattr(booker_mod, "PREP_MARGIN", 0.1)
    monkeypatch.setattr(booker_mod, "LOGIN_RETRY", 0.1)


def _plan(fire_at, login_at, **kw):
    return Plan(venue=_VENUE, date="2026-11-03", times=[("19:00", None)], courts=[5],
                fire_at=fire_at, login_at=login_at, captcha_lead=0.2, fire_delay=0.05, give_up_after=10, **kw)


def test_midnight_start_rehearses_sso_then_logs_in_at_0730_then_books(fast):
    now = time.time()
    # As at midnight: token dies before 08:00 and the 2-hour refresh token too.
    client = LoginClient(Auth(jwt(now + 1.0), jwt(now + 0.5)))
    fire_at = datetime.now(TZ) + timedelta(seconds=1.6)
    login_at = datetime.now(TZ) + timedelta(seconds=0.6)

    result = Booker(client, FakeDesk(), _plan(fire_at, login_at)).run()

    assert result and result.slot.court == "羽05"
    kinds = [e[0] for e in client.events]
    # 1) startup rehearsal of the SSO login, 2) early login check at "07:30", 3) booking.
    assert kinds[0] == "login_user" and kinds[1] == "relogin"
    t_relogin = client.events[1][1]
    assert t_relogin < login_at.timestamp()
    later_checks = [e[1] for e in client.events if e[0] == "login_user" and e[1] >= login_at.timestamp() - 0.01]
    assert later_checks, "expected a login check at login_at"
    assert len(client.reserve_bodies) == 1


def test_startup_fails_fast_when_automatic_login_is_broken(fast):
    now = time.time()
    client = LoginClient(Auth(jwt(now + 1.0), jwt(now + 0.5)), relogin_ok=False)
    fire_at = datetime.now(TZ) + timedelta(seconds=3)
    with pytest.raises(AuthError, match="Automatic login doesn't work"):
        Booker(client, FakeDesk(), _plan(fire_at, None)).run()
    assert client.reserve_bodies == []


def test_early_login_retries_until_it_works(fast):
    now = time.time()
    # Token is fine at startup but expires before opening; the first early renewal fails.
    client = LoginClient(Auth(jwt(now + 1.2), jwt(now + 9999)), renew_results=[True, False, True])
    fire_at = datetime.now(TZ) + timedelta(seconds=1.8)
    login_at = datetime.now(TZ) + timedelta(seconds=0.5)
    alerts = []
    b = Booker(client, FakeDesk(), _plan(fire_at, login_at))
    b.alert = alerts.append

    # Startup renewal (True) happens via renew_login since the refresh token outlives the opening.
    # Make the token expire again so the 07:30 check must renew (False, then True).
    orig = b._scheduled_login

    def expire_then_login(fire_local, until):
        client.auth = Auth(jwt(time.time() - 1), client.auth.refresh_token)
        orig(fire_local, until)

    b._scheduled_login = expire_then_login
    result = b.run()

    renews = [e for e in client.events if e[0] == "renew"]
    assert [r[2] for r in renews][:3] == [True, False, True]
    assert alerts and "login failed" in alerts[0]
    assert result is not None


def test_alarm_is_off_for_dry_runs_and_non_mac(monkeypatch):
    plan = _plan(datetime.now(TZ) + timedelta(seconds=5), None, alarm=True, dry_run=True)
    assert not Booker(LoginClient(None), None, plan).alarm.enabled
    monkeypatch.setattr(booker_mod.sys, "platform", "linux")
    assert not _Alarm(True).enabled


def test_alarm_rings_and_stops(monkeypatch):
    calls = []
    monkeypatch.setattr(booker_mod.sys, "platform", "darwin")
    monkeypatch.setattr(booker_mod.subprocess, "run", lambda cmd, **kw: calls.append(cmd[0]))
    a = _Alarm(True)
    a.start("wake up")
    time.sleep(0.2)
    a.stop()
    n = len(calls)
    time.sleep(1.7)
    assert calls[0] == "say" and "afplay" in calls and "caffeinate" in calls
    assert len(calls) <= n + 2  # at most the in-flight iteration finishes


# --------------------------------------------------------------------- CLI
def test_login_time_is_on_the_booking_morning_and_validated():
    fire = datetime(2026, 11, 1, 8, 0, tzinfo=TZ)
    assert cli._login_time(fire, "07:30") == datetime(2026, 11, 1, 7, 30, tzinfo=TZ)
    assert cli._login_time(fire, "off") is None
    assert cli._login_time(None, "07:30") is None
    with pytest.raises(SystemExit):
        cli._login_time(fire, "07:58")


def test_book_cli_builds_waiting_immediate_and_default_plans(monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[site]\nbase_url='https://book.test.local'\nsso_host='id.test.local'\n"
        "app_id='0'\nsign_key='0'\n"
        "[venues.main]\nname='Main'\nscene_uuid='s'\ndev_kind_uuid='d'\nclass_type_uuid='c'\n")
    config_path.chmod(0o600)
    monkeypatch.setenv("VB_CONFIG", str(config_path))
    monkeypatch.setattr(cli, "_client", lambda: SimpleNamespace())
    plans = []

    class FakeBooker:
        def __init__(self, client, desk, plan):
            plans.append(plan)

        def run(self):
            return True

    monkeypatch.setattr(cli, "Booker", FakeBooker)
    runner = CliRunner()

    def invoke(*args):
        result = runner.invoke(cli.app, ["book", "--dry-run", *args])
        assert result.exit_code == 0, result.output
        return plans[-1]

    waiting = invoke("--time", "19:00")
    assert waiting.fire_at.strftime("%H:%M") == "08:00"
    assert waiting.login_at.strftime("%H:%M") == "07:30"
    assert waiting.captcha_lead == 120

    immediate = invoke("--time", "19:00", "--now")
    assert immediate.fire_at is None and immediate.login_at is None

    defaulted = invoke("--auto-captcha")
    assert defaulted.times == [("12:00", None)] and defaulted.courts == [4, 5]
