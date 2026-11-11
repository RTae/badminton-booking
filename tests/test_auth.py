"""Login renewal: refresh token -> automatic SSO login hook -> give up."""

import json

import pytest

import browser_login
from auth import Auth
from client import AuthError, VenueClient


class FakeResponse:
    def __init__(self, payload):
        self.text = json.dumps(payload)
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class FakeSession:
    """Answers 'login expired' to the old token and success to the new one."""

    def __init__(self):
        self.headers = {}
        self.calls = []

    def request(self, method, url, params=None, headers=None, data=None, timeout=None):
        self.calls.append((url.rsplit("/site", 1)[1], dict(headers or {})))
        if url.endswith("/system/login/refreshToken"):
            return FakeResponse({"code": 401, "message": "refresh token expired"})
        if (headers or {}).get("token") == "new.jwt.token":
            return FakeResponse({"code": 0, "data": {"id": "42"}})
        return FakeResponse({"code": 401, "message": "token expired"})


def test_expired_token_falls_back_to_relogin_and_retries():
    relogins = []
    c = VenueClient(Auth("old.jwt.token", "old-refresh"), persist_auth=False,
                    relogin=lambda: relogins.append(1) or Auth("new.jwt.token", "new-refresh"))
    c.session = FakeSession()

    assert c.login_user() == {"id": "42"}
    assert relogins == [1]
    assert c.auth.token == "new.jwt.token"
    paths = [p for p, _ in c.session.calls]
    assert paths == ["/system/login/getLoginUser", "/system/login/refreshToken", "/system/login/getLoginUser"]


def test_no_saved_login_at_all_uses_relogin_first():
    c = VenueClient(None, persist_auth=False, relogin=lambda: Auth("new.jwt.token"))
    c.session = FakeSession()
    assert c.login_user() == {"id": "42"}


def test_relogin_failure_raises_auth_error():
    c = VenueClient(Auth("old.jwt.token"), persist_auth=False, relogin=lambda: None)
    c.session = FakeSession()
    with pytest.raises(AuthError):
        c.login_user()


# ------------------------------------------------------------- auto_login
class UrlClient:
    def cas_login_url(self, redirect):
        return "https://id.example.invalid/login"


def test_auto_login_headless_success(monkeypatch, tmp_path):
    monkeypatch.setenv("VB_BROWSER_PROFILE", str(tmp_path))  # profile exists -> try headless
    seen = []
    monkeypatch.setattr(browser_login, "browser_login",
                        lambda url, headless, **kw: seen.append(headless) or Auth("a.b.c", "r"))
    assert browser_login.auto_login(UrlClient(), interactive=False, log=lambda m: None).token == "a.b.c"
    assert seen == [True]


def test_auto_login_needs_person_noninteractive_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("VB_BROWSER_PROFILE", str(tmp_path))

    def fake(url, headless, **kw):
        raise browser_login.LoginNeedsYou("SSO wants your password")

    monkeypatch.setattr(browser_login, "browser_login", fake)
    assert browser_login.auto_login(UrlClient(), interactive=False, log=lambda m: None) is None


def test_auto_login_falls_back_to_window_when_interactive(monkeypatch, tmp_path):
    monkeypatch.setenv("VB_BROWSER_PROFILE", str(tmp_path))
    seen, notes = [], []

    def fake(url, headless, **kw):
        seen.append(headless)
        if headless:
            raise browser_login.LoginNeedsYou("SSO wants extra verification (2FA)")
        return Auth("x.y.z")

    monkeypatch.setattr(browser_login, "browser_login", fake)
    auth = browser_login.auto_login(UrlClient(), interactive=True, log=lambda m: None, notify=notes.append)
    assert auth.token == "x.y.z" and seen == [True, False] and notes


def test_auto_login_first_time_skips_headless(monkeypatch, tmp_path):
    monkeypatch.setenv("VB_BROWSER_PROFILE", str(tmp_path / "missing"))
    monkeypatch.setattr(browser_login, "load_credentials", lambda: None)
    seen = []
    monkeypatch.setattr(browser_login, "browser_login",
                        lambda url, headless, **kw: seen.append(headless) or Auth("x.y.z"))
    assert browser_login.auto_login(UrlClient(), interactive=True, log=lambda m: None)
    assert seen == [False]


# ------------------------------------------------------------ _read_token
class FakePage:
    def __init__(self, url, values):
        self.url, self.values = url, values

    def evaluate(self, script):
        return self.values


def test_read_token_unwraps_json_strings():
    page = FakePage("https://booking.example.invalid/venue/index.html#/home", ['"a.b.c"', '"refresh-1"'])
    assert browser_login._read_token(page) == Auth("a.b.c", "refresh-1")


def test_read_token_ignores_other_hosts_and_garbage():
    assert browser_login._read_token(FakePage("https://id.example.invalid/x", ['"a.b.c"', None])) is None
    assert browser_login._read_token(FakePage("https://booking.example.invalid/venue/", [None, None])) is None
    assert browser_login._read_token(FakePage("https://booking.example.invalid/venue/", ['""', None])) is None
