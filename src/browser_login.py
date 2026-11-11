"""Get a venue login token automatically through your institution's SSO in a browser.

The SSO sign-in page encrypts the password in the page, fingerprints the
device, sometimes shows an image captcha and may ask for 2FA, so we don't fake
it with raw HTTP. Instead we drive your installed Chrome with its own profile
(``~/.config/venue-booking/browser-profile``):

* first time: a Chrome window opens and you log in (captcha / 2FA if asked);
* afterwards: the profile remembers the SSO session and trusted device, so a
  fresh token is fetched headlessly with no interaction;
* optionally your login id + password live in a config file (see
  :mod:`config`) or the macOS Keychain, and we
  type them into the SSO form for you. We only press Enter when no captcha is
  showing; a captcha or 2FA is always left for you to complete in the window.

Once SSO sends the browser back to the venue site, the site's own JavaScript
exchanges the ticket for ``token`` / ``refreshToken`` and stores them in
``localStorage``; we read them from there.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal

import config
from auth import Auth

if TYPE_CHECKING:
    from client import VenueClient

KEYRING_SERVICE = "venue-booking (SSO)"
_USERNAME_ACCOUNT = "__username__"

DEFAULT_PROFILE = config.CONFIG_DIR / "browser-profile"


class LoginNeedsYou(RuntimeError):
    """Raised in headless mode when SSO wants something only a person can give."""


def _venue_origin() -> str:
    return config.site().origin


def _venue_index() -> str:
    return config.site().index_url


def _sso_host() -> str:
    return config.site().sso_host


def redirect_url() -> str:
    return f"{_venue_index()}#/blank"


def profile_dir() -> Path:
    return Path(os.environ.get("VB_BROWSER_PROFILE", DEFAULT_PROFILE))


# ---------------------------------------------------------------- keychain
def load_credentials() -> tuple[str, str] | None:
    """SSO username + password: the config file first, then the Keychain."""
    return config.sso_credentials() or keychain_credentials()


def credentials_source() -> str:
    if config.sso_credentials():
        return f"config file {config.config_path()}"
    if keychain_credentials():
        return "macOS Keychain"
    return "none"


def keychain_credentials() -> tuple[str, str] | None:
    import keyring

    try:
        user = keyring.get_password(KEYRING_SERVICE, _USERNAME_ACCOUNT)
        if not user:
            return None
        pw = keyring.get_password(KEYRING_SERVICE, user)
    except keyring.errors.KeyringError:
        return None
    return (user, pw) if pw else None


def save_credentials(username: str, password: str) -> None:
    import keyring

    keyring.set_password(KEYRING_SERVICE, _USERNAME_ACCOUNT, username)
    keyring.set_password(KEYRING_SERVICE, username, password)


def forget_credentials() -> bool:
    import keyring

    creds = keychain_credentials()
    if not creds:
        return False
    for account in (creds[0], _USERNAME_ACCOUNT):
        try:
            keyring.delete_password(KEYRING_SERVICE, account)
        except keyring.errors.PasswordDeleteError:
            pass
    return True


# ------------------------------------------------------------------- login
def _launch(pw, headless: bool):
    path = profile_dir()
    path.mkdir(parents=True, exist_ok=True)
    kwargs = dict(user_data_dir=str(path), headless=headless, locale="zh-CN",
                  viewport={"width": 1100, "height": 800})
    try:
        return pw.chromium.launch_persistent_context(channel="chrome", **kwargs)
    except Exception as chrome_error:  # Chrome not installed: try Playwright's own Chromium
        try:
            return pw.chromium.launch_persistent_context(**kwargs)
        except Exception:
            raise RuntimeError(
                "Couldn't start a browser. Install Google Chrome, or run "
                "`uv run playwright install chromium`."
            ) from chrome_error


def _read_token(page) -> Auth | None:
    if not page.url.startswith(_venue_origin()):
        return None
    try:
        raw_token, raw_refresh = page.evaluate(
            "[localStorage.getItem('token'), localStorage.getItem('refreshToken')]")
    except Exception:  # page is mid-navigation
        return None

    def unwrap(v):
        if not v:
            return ""
        try:
            v = json.loads(v)  # the site stores JSON.stringify(value)
        except ValueError:
            pass
        return v if isinstance(v, str) else ""

    token = unwrap(raw_token)
    return Auth(token=token, refresh_token=unwrap(raw_refresh)) if token.count(".") == 2 else None


def _sso_state(page) -> Literal["form", "captcha", "other", "away"]:
    """'form', 'captcha' (form with image captcha), 'other' (2FA etc.) or 'away'."""
    if _sso_host() not in page.url:
        return "away"
    try:
        password_field = page.locator("#i_pass")
        if not password_field.is_visible():
            tab = page.locator("h3[data-id='theform']")
            tab_first = tab.first
            if tab.count() and tab_first.is_visible():
                tab_first.click()
        if password_field.is_visible():
            captcha = page.locator("#c_code")
            if captcha.count():
                captcha_first = captcha.first
                classes = captcha_first.get_attribute("class") or ""
                if captcha_first.is_visible() and "hidden" not in classes:
                    return "captcha"
            return "form"
    except Exception:
        return "other"
    return "other"


def _fill(page, creds: tuple[str, str], submit: bool) -> None:
    user, password = creds
    if page.locator("#i_user").get_attribute("readonly") is None:
        page.fill("#i_user", user)
    page.fill("#i_pass", password)
    remember = page.locator("#theform input[name='singleLogin']")
    if remember.count() and not remember.first.is_checked():
        remember.first.check()
    if submit:
        page.press("#i_pass", "Enter")  # the page's own keyLogin() -> doLogin()


def browser_login(
    login_url: str,
    *,
    headless: bool,
    timeout: float = 300.0,
    log: Callable[[str], None] = print,
) -> Auth:
    """Log in via SSO and return a fresh token pair.

    ``login_url`` comes from :meth:`VenueClient.cas_login_url`. Headless mode
    raises :class:`LoginNeedsYou` as soon as a person is required.
    """
    from playwright.sync_api import sync_playwright

    creds = load_credentials()
    with sync_playwright() as pw:
        ctx = _launch(pw, headless)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            # Drop any old venue session in this profile so we only read a fresh token.
            page.goto(_venue_index(), wait_until="domcontentloaded")
            page.evaluate("['token','refreshToken','userInfo','headers'].forEach(k => localStorage.removeItem(k))")
            page.goto(login_url, wait_until="domcontentloaded")

            deadline = time.time() + (min(timeout, 45) if headless else timeout)
            submitted_at: float | None = None
            stuck_since: float | None = None
            hinted: set[str] = set()

            def hint(key: str, msg: str) -> None:
                if key not in hinted:
                    hinted.add(key)
                    log(msg)

            while time.time() < deadline:
                auth = _read_token(page)
                if auth:
                    return auth
                state = _sso_state(page)
                if state == "away":                       # redirects in flight / venue page loading
                    stuck_since = None
                elif state == "form" and creds and submitted_at is None:
                    hint("fill", "SSO asked for your password — filling in your saved one")
                    _fill(page, creds, submit=True)
                    submitted_at = time.time()
                elif headless:
                    stuck_since = stuck_since or time.time()
                    if state == "captcha":
                        raise LoginNeedsYou("SSO is showing an image captcha")
                    if state == "form" and not creds:
                        raise LoginNeedsYou("SSO wants your password")
                    if state == "form" and time.time() - submitted_at > 10:
                        raise LoginNeedsYou("SSO didn't accept the saved password")
                    if state == "other" and time.time() - stuck_since > 8:
                        raise LoginNeedsYou("SSO wants extra verification (2FA)")
                else:
                    if state == "captcha":
                        if creds and submitted_at is None:
                            _fill(page, creds, submit=False)
                            submitted_at = time.time()
                        hint("captcha", "Type the captcha in the browser window and press 登录")
                    elif state == "form":
                        hint("form", "Log in in the browser window (tick 统一登录 to stay signed in)")
                    else:
                        hint("other", "Finish the verification in the browser window "
                                      "(choose to trust this browser if offered)")
                page.wait_for_timeout(400)
            raise TimeoutError("Timed out waiting for the login to finish")
        finally:
            ctx.close()


def auto_login(
    client: VenueClient,
    *,
    interactive: bool,
    force_visible: bool = False,
    timeout: float = 300.0,
    log: Callable[[str], None] = print,
    notify: Callable[[str], None] | None = None,
) -> Auth | None:
    """Headless SSO login if possible; otherwise (when ``interactive``) open a window.

    ``client`` is a :class:`VenueClient`, used only to ask the site for the SSO URL.
    Returns ``None`` instead of raising, so callers can fall back gracefully.
    """
    try_headless = not force_visible and (profile_dir().exists() or load_credentials() is not None)
    if try_headless:
        try:
            log("logging in through SSO in the background…")
            auth = browser_login(client.cas_login_url(redirect_url()), headless=True, log=log)
            log("✓ logged in automatically")
            return auth
        except LoginNeedsYou as e:
            if not interactive:
                log(f"automatic login needs you ({e}); run `venue-booking auth`")
                return None
            log(f"{e} — opening a Chrome window so you can log in")
        except Exception as e:  # browser/network trouble: don't crash the caller
            log(f"automatic login failed: {e}")
            if not interactive:
                return None
    elif not interactive:
        log("no saved browser login yet; run `venue-booking auth` once")
        return None

    if notify:
        notify("Log in to your SSO in the Chrome window")
    try:
        auth = browser_login(client.cas_login_url(redirect_url()), headless=False, timeout=timeout, log=log)
    except Exception as e:
        log(f"login didn't complete: {e}")
        return None
    log("✓ logged in — next time this can happen in the background")
    return auth
