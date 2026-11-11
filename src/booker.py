"""The booking run: wait for opening time, then grab the best matching court."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

import config
from auth import jwt_expiry
from captcha_desk import CaptchaDesk
from client import ApiError, AuthError, Venue, VenueClient
from slots import Slot, parse_slots, rank_candidates

TZ = ZoneInfo("Asia/Shanghai")

# A solved captcha stays valid on the server for 3 minutes; keep a margin.
VERIFICATION_TTL = 150.0
# Right after opening, slots may still read "not open" for a moment.
OPENING_GRACE = 15.0
# Final preparation (login check, pre-load) starts at least PREP_MIN before opening,
# and at least PREP_MARGIN before the captcha is asked for.
PREP_MIN = 120.0
PREP_MARGIN = 30.0
# Wait between early-login attempts when one fails.
LOGIN_RETRY = 120.0

# When the first post-opening court list is requested, relative to fire time + fire_delay.
RACE_DELAYS = (0.0, 0.2, 0.45)

# Substrings in addReserve error messages.
_CAPTCHA_ERROR_HINTS = ("验证", "captcha", "Captcha", "滑块")
_NOT_OPEN_HINTS = ("未开放", "未到", "尚未")


@dataclass
class Plan:
    venue: Venue
    date: str
    times: list[tuple[str, str | None]]
    courts: list[int] | None
    pay_type: str = "PAY_ONLINE"
    fire_at: datetime | None = None      # aware datetime (server clock)
    login_at: datetime | None = None     # renew the login at this time (aware, server clock)
    captcha_lead: float = 60.0           # seconds before fire_at to ask for the captcha
    fire_delay: float = 0.1              # seconds after fire_at to send the first request
    give_up_after: float = 180.0         # seconds after fire_at to stop trying
    dry_run: bool = False
    alarm: bool = False                  # ring until the captcha is solved (macOS)


@dataclass
class Result:
    slot: Slot
    resv_ids: list[str]
    order: dict | None = None


@dataclass
class _Verification:
    value: str
    solved_at: float = field(default_factory=time.time)

    @property
    def fresh(self) -> bool:
        return time.time() - self.solved_at < VERIFICATION_TTL


def _stamp() -> str:
    return datetime.now(TZ).strftime("%H:%M:%S.%f")[:-3]


class _Alarm:
    """Wake the display and ring until stopped (macOS only)."""

    SOUND = "/System/Library/Sounds/Glass.aiff"

    def __init__(self, enabled: bool):
        self.enabled = enabled and sys.platform == "darwin"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, spoken: str) -> None:
        if not self.enabled or self._thread:
            return
        stop = self._stop = threading.Event()  # one per ring, so an old ring can't be revived

        def ring() -> None:
            subprocess.run(["say", spoken], check=False, capture_output=True)
            while not stop.is_set():
                subprocess.run(["caffeinate", "-u", "-t", "2"], check=False, capture_output=True)  # screen on
                subprocess.run(["afplay", self.SOUND], check=False, capture_output=True)
                stop.wait(1.5)

        self._thread = threading.Thread(target=ring, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread = None


class Booker:
    def __init__(self, client: VenueClient, desk: CaptchaDesk | None, plan: Plan):
        self.client = client
        self.desk = desk
        self.plan = plan
        self._deploy_cache: dict[str, str] = {}
        self._caffeinate: subprocess.Popen | None = None
        self.alarm = _Alarm(plan.alarm and not plan.dry_run)
        self._page_raised = False

    # ---------------------------------------------------------------- utils
    def log(self, msg: str) -> None:
        line = f"[{_stamp()}] {msg}"
        print(line, flush=True)
        if self.desk:
            self.desk.log(line)

    def notify(self, msg: str) -> None:
        if sys.platform == "darwin":
            subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "Court booking" sound name "Glass"'],
                           check=False, capture_output=True)

    def _sleep_until(self, local_epoch: float) -> None:
        while True:
            left = local_epoch - time.time()
            if left <= 0:
                return
            time.sleep(min(left - 0.02, 30) if left > 0.05 else 0.001)

    def alert(self, msg: str) -> None:
        """Notification, plus a spoken message when the alarm is on (for things that need you)."""
        self.notify(msg)
        if self.alarm.enabled:
            subprocess.Popen(["say", msg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _keep_awake(self) -> None:
        if sys.platform == "darwin" and not self._caffeinate:
            self._caffeinate = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])

    def _deploy_uuid(self, form_uuid: str) -> str:
        if form_uuid not in self._deploy_cache:
            self._deploy_cache[form_uuid] = self.client.workflow_deploy_uuid(form_uuid)
        return self._deploy_cache[form_uuid]

    def _fetch_slots(self, day: str | None = None) -> list[Slot]:
        day = day or self.plan.date
        return parse_slots(self.client.list_sites(self.plan.venue, day), day)

    def _fetch_slots_race(self, delays: tuple[float, ...] = RACE_DELAYS) -> list[Slot]:
        """Staggered list requests right at opening; return the first non-empty answer.

        The list endpoint takes 1-2 s and returns nothing for the new day until
        booking opens, so a request that lands a hair early would otherwise cost
        a whole extra round trip.
        """
        start = time.time()

        def job(delay: float) -> list[Slot]:
            time.sleep(max(0.0, start + delay - time.time()))
            return self._fetch_slots()

        pool = ThreadPoolExecutor(max_workers=len(delays))
        try:
            for fut in as_completed([pool.submit(job, d) for d in delays]):
                try:
                    slots = fut.result()
                except Exception as e:  # one failed request shouldn't sink the others
                    self.log(f"list request failed: {e}")
                    continue
                if slots:
                    return slots
            return []
        finally:
            pool.shutdown(wait=False, cancel_futures=True)  # don't wait for slower duplicates

    def _warm_deploy_cache(self, slots: list[Slot]) -> None:
        """Resolve the booking form's workflow id ahead of time.

        Before opening, the target day lists no courts, so borrow the form id
        from a day that is already open (it is the same for the whole venue).
        """
        if not slots:
            open_day = (datetime.fromisoformat(self.plan.date) - timedelta(days=1)).date().isoformat()
            try:
                slots = self._fetch_slots(open_day)
            except ApiError:
                return
        for form in {s.form_uuid for s in slots if s.form_uuid}:
            self._deploy_uuid(form)

    def _body(self, slot: Slot, user_id: str, verification: str) -> dict:
        when = {"startTime": f"{slot.date} {slot.begin}:00", "endTime": f"{slot.date} {slot.end}:00"}
        return {
            "sceneUuid": slot.scene_uuid,
            "sceneUseType": self.plan.venue.scene_use_type,
            "siteUuid": slot.site_uuid,
            "siteType": "DEV",
            "reserveTime": [when],
            "siteSessionReserve": [{"sessionDetailUuid": slot.session_uuid, "reserveTime": when}],
            "resvMember": [user_id],
            "resvKind": "CURRENT_RESERVE",
            "payType": self.plan.pay_type,
            "purchaseUuid": "",
            "formParam": {"formId": slot.form_uuid, "deployUuid": self._deploy_uuid(slot.form_uuid),
                          "variables": {}, "chooseCandidates": {}},
            "captcha": verification,
        }

    def _ensure_token_valid_until(self, local_epoch: float) -> None:
        exp = self.client.auth.expires_at if self.client.auth else None
        if exp is None or exp.timestamp() > local_epoch:
            return  # still valid (or no expiry claim we can read)
        if self.client.renew_login():
            new_exp = self.client.auth.expires_at
            self.log(f"renewed login (now valid until {new_exp.astimezone(TZ):%H:%M})" if new_exp else "renewed login")
            return
        raise AuthError("Login token will have expired by booking time and could not be renewed. "
                        "Run `venue-booking auth` and try again.")

    def _get_verification(self, reason: str, deadline: float | None) -> _Verification | None:
        if self.plan.dry_run:
            self.log("[dry-run] skipping captcha")
            return _Verification("<dry-run: no captcha>")
        if not self.desk:
            raise RuntimeError("captcha desk not started")
        if getattr(self.desk, "autosolve", False):
            # Let the computer vision try first; only wake anyone if it is unsure.
            self.log(f"captcha needed ({reason}) — trying auto-solve…")
            grace = time.time() + 3.0
            value = self.desk.solve(deadline=min(deadline, grace) if deadline else grace, reason=reason)
            if value:
                self.log("✓ captcha solved automatically")
                return _Verification(value)
            self.log("auto-solve not confident — ringing the alarm")
        else:
            self.log(f"captcha needed ({reason}) — solve it at {self.desk.url}")
        if self.alarm.enabled and not self._page_raised:
            webbrowser.open(self.desk.url)  # bring the page to the front
            self._page_raised = True
        self.alarm.start("Court booking. Solve the captcha.")
        try:
            value = self.desk.solve(deadline=deadline, reason=reason)
        finally:
            self.alarm.stop()
        return _Verification(value) if value else None

    # ------------------------------------------------------------------ run
    def run(self) -> Result | None:
        p = self.plan
        started = time.time()
        user = self.client.login_user()
        user_id = str(user["id"])
        self.log(f"logged in as {user.get('name') or user.get('nickName') or user.get('account') or user_id}")

        fire_local = self._prepare_scheduled_run(started)

        # Pre-load candidates. Usually the new day lists nothing until opening;
        # if it does list sessions they read "not open", which is fine.
        slots = self._fetch_slots()
        candidates = rank_candidates(slots, p.times, p.courts, include_not_open=bool(p.fire_at))
        self._warm_deploy_cache(slots)
        self._print_candidates(candidates, slots)
        if not candidates and not p.fire_at:
            self.log("nothing matching your preferences is bookable right now")
            return None

        return self._book_with_retries(user_id, fire_local, candidates)

    def _prepare_scheduled_run(self, started: float) -> float | None:
        p = self.plan
        fire_local: float | None = None
        if p.fire_at:
            offset = self.client.server_time_offset()
            fire_local = p.fire_at.timestamp() - offset
            self.log(f"server clock offset {offset:+.2f}s; booking opens {p.fire_at:%Y-%m-%d %H:%M:%S} "
                     f"({max(0, fire_local - time.time()):.0f}s from now)")
            prep_at = fire_local - max(p.captcha_lead + PREP_MARGIN, PREP_MIN)
            login_local = p.login_at.timestamp() - offset if p.login_at else None
            if login_local is not None and not (time.time() < login_local < prep_at):
                login_local = None  # already past (or too late to matter): skip the early login
            self._check_login_lasts(fire_local, next_renewal=login_local or prep_at)
            if self.desk:
                self.desk.set_headline(f"Booking {p.date} — opens {p.fire_at:%H:%M:%S}", fire_local)
            if fire_local - time.time() > 60:
                self._keep_awake()
            if login_local is not None:
                self.log(f"sleeping until {p.login_at:%H:%M} to log in…")
                self._sleep_until(login_local)
                self._scheduled_login(fire_local, until=prep_at - PREP_MARGIN)
            if time.time() < prep_at:
                self.log(f"waiting until {datetime.fromtimestamp(prep_at, TZ):%H:%M:%S} to prepare…")
                self._sleep_until(prep_at)
            if prep_at - started > 600:
                # Clocks drift over a long wait: measure the server offset again.
                offset = self.client.server_time_offset()
                fire_local = p.fire_at.timestamp() - offset
                self.log(f"server clock offset re-measured: {offset:+.2f}s")
                if self.desk:
                    self.desk.set_headline(f"Booking {p.date} — opens {p.fire_at:%H:%M:%S}", fire_local)
            self._ensure_token_valid_until(fire_local + 600)
        elif self.desk:
            self.desk.set_headline(f"Booking {p.date} now")
        return fire_local

    def _book_with_retries(
        self, user_id: str, fire_local: float | None, candidates: list[Slot]
    ) -> Result | None:
        p = self.plan
        verification: _Verification | None = None
        if fire_local is not None:
            self._sleep_until(fire_local - p.captcha_lead)
            verification = self._get_verification("ready for opening", deadline=fire_local + p.give_up_after)
            if not verification:
                self.log("no captcha solved in time; giving up")
                return None
            self.log(f"captcha ready; firing at {p.fire_at:%H:%M:%S} + {p.fire_delay:.2f}s")
            self._sleep_until(fire_local - 1.5)
            self.client.login_user()  # keep the HTTPS connection warm
            self._sleep_until(fire_local + p.fire_delay)
            give_up = fire_local + p.give_up_after
        else:
            give_up = time.time() + p.give_up_after

        tried: set[str] = set()
        use_preloaded = bool(candidates)
        first_fetch = True
        while time.time() < give_up:
            in_grace = fire_local is not None and time.time() - fire_local < OPENING_GRACE
            if verification is None or not verification.fresh:
                verification = self._get_verification("next attempt", deadline=give_up)
                if not verification:
                    break
            if not use_preloaded:
                try:
                    slots = self._fetch_slots_race() if (first_fetch and in_grace) else self._fetch_slots()
                except requests.RequestException as e:
                    self.log(f"court list request failed ({e.__class__.__name__}); retrying")
                    time.sleep(0.5)
                    continue
                first_fetch = False
                candidates = rank_candidates(slots, p.times, p.courts, include_not_open=in_grace)
            use_preloaded = False
            pool = [c for c in candidates if c.session_uuid not in tried]
            if not pool:
                if in_grace:
                    time.sleep(0.3)
                    continue
                self.log("no matching slots left")
                return None

            slot = pool[0]
            if p.dry_run:
                self.log(f"[dry-run] would book {slot.label()} — stopping before addReserve")
                return Result(slot, resv_ids=[])
            self.log(f"→ booking {slot.label()}")
            try:
                data = self.client.add_reserve(self._body(slot, user_id, verification.value))
            except ApiError as e:
                msg = str(e)
                self.log(f"✗ {slot.label()}: {msg}")
                if any(k in msg for k in _CAPTCHA_ERROR_HINTS):
                    verification = None          # captcha used up / rejected: ask for a new one
                elif in_grace and any(k in msg for k in _NOT_OPEN_HINTS):
                    time.sleep(0.2)              # opened a hair late: retry the same slot
                else:
                    tried.add(slot.session_uuid)  # taken or not allowed: move on
                continue
            except requests.RequestException as e:
                # No answer: the booking may or may not have gone through.
                self.log(f"? {slot.label()}: no answer from the site ({e.__class__.__name__}); checking…")
                if self._booked_by_me(slot, user_id):
                    self.log(f"✓ it went through: {slot.label()} is reserved under your account")
                    return self._finish(Result(slot, resv_ids=[]), user_id)
                continue  # not booked: retry (the list is re-read first)

            resv_ids = data.get("resvIds") or []
            self.log(f"✓ reserved {slot.label()} (reservation {', '.join(resv_ids)})")
            return self._finish(Result(slot, resv_ids), user_id)

        self.log("gave up: time limit reached")
        return None

    def _check_login_lasts(self, fire_local: float, next_renewal: float) -> None:
        """Fail now, not at 07:30, if the login can't survive until booking time.

        ``next_renewal`` is when the script will next renew the login (the
        early login, or the final check before opening).
        """
        auth = self.client.auth
        exp = auth.expires_at if auth else None
        if not exp or exp.timestamp() > fire_local + 600:
            return
        refresh_exp = jwt_expiry(auth.refresh_token) if auth.refresh_token else None
        refresh_dead_by_then = refresh_exp is None or refresh_exp.timestamp() < next_renewal
        relogin = getattr(self.client, "relogin", None)
        if refresh_dead_by_then and relogin:
            # The refresh token (≈2 h) will be gone by then, so the later login
            # must go through SSO. Rehearse that now, while you're still awake.
            self.log("checking that automatic SSO login works (it will be needed later)…")
            new = relogin()
            if not new:
                raise AuthError("Automatic login doesn't work right now. Run `venue-booking auth`, "
                                "finish any 2FA in the window, then start the booking again.")
            self.client._set_auth(new)
            self.log("✓ automatic SSO login works")
            return
        # The token won't last until opening. Prove now that we can renew it
        # (refresh token or automatic SSO login), and renew again at 07:58.
        if not self.client.renew_login():
            raise AuthError(f"Your login token expires at {exp.astimezone(TZ):%H:%M}, before booking opens, "
                            "and it couldn't be renewed. Run `venue-booking auth` and try again.")
        new_exp = self.client.auth.expires_at
        self.log("login renewed OK" + (f" (valid until {new_exp.astimezone(TZ):%H:%M}; "
                                         "will renew again before opening)" if new_exp else ""))

    def _scheduled_login(self, fire_local: float, until: float) -> None:
        """The early login (default 07:30): make sure a fresh token covers the opening.

        Retries every 2 minutes on failure; the final check at 07:58 still runs.
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                self._ensure_token_valid_until(fire_local + 3600)
                self.client.login_user()  # the server agrees the token works
                exp = self.client.auth.expires_at if self.client.auth else None
                self.log("✓ logged in for the opening" + (f" (token valid until {exp.astimezone(TZ):%H:%M})" if exp else ""))
                return
            except (AuthError, ApiError, requests.RequestException) as e:
                self.log(f"login attempt {attempt} failed: {e}")
                if attempt == 1:
                    self.alert("Court booking: automatic login failed, retrying")
            if time.time() + LOGIN_RETRY > until:
                self.log("couldn't log in early; one more try right before opening")
                return
            time.sleep(LOGIN_RETRY)

    def _booked_by_me(self, slot: Slot, user_id: str) -> bool:
        try:
            fresh = self._fetch_slots()
        except (ApiError, requests.RequestException):
            return False
        return any(s.session_uuid == slot.session_uuid and user_id in s.booked_by for s in fresh)

    def _finish(self, result: Result, user_id: str) -> Result:
        if not result.resv_ids:
            self.log("couldn't read the reservation id — check 我的预约 / 我的订单 on the website")
            self._wrap_up(result)
            return result
        try:
            check = self.client.order_check(result.resv_ids, user_id)
            result.order = self.client.get_order(result.resv_ids[0])
            o = result.order or {}
            amount = (o.get("payableAmount") or 0) / 100
            self.log(f"order {o.get('orderNo', '?')}: ¥{amount:.2f}, status {o.get('orderStatus', '?')}, "
                     f"pay before {o.get('payDeadline', '?')}  (orderGenerated={check.get('orderGenerated')})")
        except (ApiError, requests.RequestException) as e:
            self.log(f"reserved, but fetching the order failed: {e}")
        self._wrap_up(result)
        return result

    def _wrap_up(self, result: Result) -> None:
        if self.plan.pay_type == "PAY_ONLINE":
            self.log("➜ Pay online within 60 minutes or it is cancelled: "
                     f"{config.site().index_url} → 个人中心 / 我的订单")
        self.notify(f"Booked {result.slot.label()} — remember to pay")
        if self.desk:
            self.desk.set_headline(f"✓ Booked {result.slot.label()}")

    def _print_candidates(self, candidates: list[Slot], slots: list[Slot]) -> None:
        if not slots:
            self.log(f"no courts listed for {self.plan.date} yet")
            return
        if not candidates:
            self.log("no slots match your time/court preferences")
            return
        top = ", ".join(f"{c.begin}-{c.end} {c.court}" + ("" if c.bookable else " (not open yet)") for c in candidates[:6])
        more = f" (+{len(candidates) - 6} more)" if len(candidates) > 6 else ""
        self.log(f"{len(candidates)} candidate slots, in order: {top}{more}")
