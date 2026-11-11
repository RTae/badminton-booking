"""Command line entry point: ``venue-booking <command>``."""

from __future__ import annotations

import getpass
import os
import sys
import time
from pathlib import Path
from datetime import date as Date, datetime, time as Time, timedelta
from enum import Enum
from typing import Annotated

import typer

import auth as authmod
import config
from booker import TZ, Booker, Plan, Result, _Alarm
from browser_login import auto_login, credentials_source, forget_credentials, save_credentials
from captcha_desk import CaptchaDesk
from client import ApiError, AuthError, Venue, VenueClient
from slots import (
    REASON_ENDED,
    REASON_FULL,
    REASON_NOT_OPEN,
    Slot,
    parse_court_pref,
    parse_slots,
    parse_time_pref,
)

app = typer.Typer(
    name="venue-booking",
    help="Book courts on a venue booking site (configure the site in ~/.config/venue-booking/config.toml).",
    rich_markup_mode="markdown",
    no_args_is_help=True,
    add_completion=False,
)


class PayMethod(str, Enum):
    online = "online"
    offline = "offline"


def _venue_name(value: str | None) -> str | None:
    if value is None:
        return None
    keys = config.venues()
    if value not in keys:
        raise typer.BadParameter(f"must be one of: {', '.join(sorted(keys))}")
    return value


def _resolve_venue(key: str | None) -> Venue:
    venues = config.venues()
    return venues[key or next(iter(venues))]


def _captcha_lead(value: float) -> float:
    if value > 150:
        raise typer.BadParameter("must be ≤ 150s: a solved captcha only stays valid ~3 minutes")
    return value


VenueOption = Annotated[str | None, typer.Option(
    help="which venue from your config's [venues] (default: the first one)", callback=_venue_name,
)]
CaptchaLead = Annotated[float, typer.Option(
    help="ring and ask for the captcha this many seconds before opening (default 120, max 150)",
    callback=_captcha_lead,
)]
NoBrowserOption = Annotated[bool, typer.Option(
    "--no-browser", help="don't auto-open the page",
)]


# ---------------------------------------------------------------- helpers
def _resolve_date(text: str, base: Date) -> str:
    """``+3`` (days after ``base``), ``today``, ``tomorrow`` or ``YYYY-MM-DD``."""
    text = text.strip().lower()
    if text in ("today", "+0"):
        return base.isoformat()
    if text == "tomorrow":
        return (base + timedelta(days=1)).isoformat()
    if text.startswith("+"):
        return (base + timedelta(days=int(text[1:]))).isoformat()
    return Date.fromisoformat(text).isoformat()


def _parse_hhmm(text: str) -> Time:
    h, m = (int(x) for x in text.split(":"))
    return Time(h, m)


def _next_occurrence(hhmm: str) -> datetime:
    now = datetime.now(TZ)
    target = datetime.combine(now.date(), _parse_hhmm(hhmm), TZ)
    return target if target > now else target + timedelta(days=1)


def _client() -> VenueClient:
    """A client that logs in by itself (refresh token, then SSO in the browser) when needed."""
    c = VenueClient(authmod.load())
    c.relogin = lambda: auto_login(c, interactive=sys.stdin.isatty())
    return c


def _fmt_exp(a: authmod.Auth) -> str:
    exp = a.expires_at
    if not exp:
        return "unknown expiry"
    left = exp - datetime.now(exp.tzinfo)
    when = exp.astimezone(TZ).strftime("%Y-%m-%d %H:%M")
    return f"expires {when} ({'EXPIRED' if left.total_seconds() < 0 else f'{left.total_seconds() / 3600:.1f}h left'})"


# --------------------------------------------------------------- commands
@app.command("auth", help="log in (automatic via your SSO in Chrome)")
def cmd_auth(
    remember_password: Annotated[bool, typer.Option(
        "--remember-password",
        help="save your SSO ID + password in the macOS Keychain so logins need no typing",
    )] = False,
    forget_password: Annotated[bool, typer.Option("--forget-password", help="remove the saved password")] = False,
    visible: Annotated[bool, typer.Option("--visible", help="always show the browser window")] = False,
    paste: Annotated[bool, typer.Option("--paste", help="old way: paste the token copied from DevTools")] = False,
    token: Annotated[str | None, typer.Option(help="set the JWT directly (from localStorage 'token')")] = None,
    refresh_token: Annotated[str | None, typer.Option(help="with --token: localStorage 'refreshToken'")] = None,
) -> None:
    if forget_password:
        print("Removed the saved password from the Keychain." if forget_credentials() else "No saved password.")
        return
    if remember_password:
        user = input("Student / staff ID: ").strip()
        password = getpass.getpass("SSO password (stored in the macOS Keychain): ")
        if not user or not password:
            raise SystemExit("Nothing saved.")
        save_credentials(user, password)
        print("Saved. The script will type these into the SSO page when it needs to log in.")

    if token:
        a = authmod.Auth(token=token, refresh_token=refresh_token or "")
    elif not paste:
        a = auto_login(VenueClient(None), interactive=True, force_visible=visible, timeout=600)
        if not a:
            raise SystemExit("Login didn't complete. Try `venue-booking auth --visible`, or `--paste`.")
    else:
        print(f"1. Log in at {config.site().index_url} (Chrome/Edge).")
        print("2. Open DevTools → Console, paste this line and press Enter (it copies to your clipboard):\n")
        print("   " + authmod.BROWSER_SNIPPET + "\n")
        print("3. Paste the result here and press Enter:")
        text = sys.stdin.readline()
        if not text.strip():
            raise SystemExit("Nothing pasted.")
        a = authmod.parse_pasted(text)
    path = authmod.save(a)
    print(f"Saved to {path}  — token {_fmt_exp(a)}; refresh token: {'yes' if a.refresh_token else 'NO'}")
    try:
        user = VenueClient(a).login_user()
        print(f"Logged in as {user.get('name') or user.get('nickName') or a.username} (user id {user['id']})")
    except (ApiError, AuthError) as e:
        print(f"Warning: the token did not work: {e}")


@app.command("whoami", help="check the saved login")
def cmd_whoami() -> None:
    c = _client()
    user = c.login_user()
    print(f"{user.get('name') or user.get('nickName')}  account={user.get('account') or c.auth.username}  id={user['id']}")
    print(f"token {_fmt_exp(c.auth)}; refresh token saved: {'yes' if c.auth.refresh_token else 'no'}; "
          f"saved SSO password: {credentials_source()}")


def _print_availability(venue: Venue, day: str, slots: list[Slot]) -> None:
    courts = sorted({s.court for s in slots}, key=lambda n: (len(n), n))
    times = sorted({(s.begin, s.end) for s in slots})
    grid = {(s.begin, s.end, s.court): s for s in slots}
    kind = "weekend" if Date.fromisoformat(day).weekday() >= 5 else "weekday"
    print(f"{venue.name}  {day} ({Date.fromisoformat(day):%a}, {kind} schedule)   "
          f"✓ free   ✗ taken   · not open   - locked by venue   (blank = already over)\n")
    print(" " * 12 + " ".join(f"{name[-2:]:>2}" for name in courts))
    for begin, end in times:
        cells = []
        for name in courts:
            slot = grid.get((begin, end, name))
            if slot is None:
                cells.append("  ")
            elif slot.bookable:
                cells.append(" ✓")
            elif slot.reason == REASON_FULL:
                cells.append(" ✗")
            elif slot.reason == REASON_NOT_OPEN:
                cells.append(" ·")
            elif slot.reason == REASON_ENDED:
                cells.append("  ")
            else:
                cells.append(" -")
        print(f"{begin}-{end} " + " ".join(cells))


@app.command("list", help="show court availability for a day")
def cmd_list(
    date: Annotated[str, typer.Option(help="+N days, today, tomorrow or YYYY-MM-DD (default +3)")] = "+3",
    venue: VenueOption = None,
) -> None:
    c = _client()
    venue = _resolve_venue(venue)
    day = _resolve_date(date, datetime.now(TZ).date())
    slots = parse_slots(c.list_sites(venue, day), day)
    if not slots:
        print(f"No courts listed for {venue.name} on {day}.")
        return
    _print_availability(venue, day, slots)


@app.command("init-config", help="create a config file (site, SSO login, booking defaults)")
def cmd_init_config(
    here: Annotated[bool, typer.Option(
        "--here",
        help="create ./config.toml in this folder instead of ~/.config/venue-booking/",
    )] = False,
) -> None:
    env = os.environ.get("VB_CONFIG")
    target = config.LOCAL_CONFIG if here else Path(env).expanduser() if env else config.USER_CONFIG
    try:
        path = config.create(target)
    except FileExistsError:
        raise SystemExit(f"{target} already exists — edit it directly.")
    print(f"Created {path.resolve()} (readable only by you).")
    print("Open it and fill in [site], [venues] and [sso]. See config.example.toml / the README.")


@app.command("try-captcha", help="practise the 07:58 alarm + captcha once (books nothing)")
def cmd_try_captcha(
    no_browser: NoBrowserOption = False,
    no_alarm: Annotated[bool, typer.Option("--no-alarm", help="practise without the alarm sound")] = False,
    auto_captcha: Annotated[bool, typer.Option(
        "--auto-captcha",
        help="let the script solve the captcha by computer vision; you only drag if it's unsure",
    )] = False,
) -> None:
    """Open the captcha page with a real challenge, so you can check it works. Books nothing."""
    desk = CaptchaDesk(VenueClient(None), open_browser=not no_browser, autosolve=auto_captcha)
    print(f"Captcha page: {desk.start()}  — solve the slider there (Ctrl-C to quit)")
    desk.set_headline("Practice run — nothing will be booked")
    alarm = _Alarm(not no_alarm and not auto_captcha)
    if alarm.enabled:
        print("Ringing the 07:58 alarm until you solve it (use --no-alarm to practise quietly).")
    elif auto_captcha:
        print("Auto-solve on: the script tries computer vision; you only drag if it's unsure.")
    alarm.start("Booking. Solve the captcha.")
    try:
        ok = desk.solve(deadline=time.time() + 300)
    finally:
        alarm.stop()
        time.sleep(1.0)  # let the page show the result
        desk.stop()
    print("✓ Captcha accepted by the site — the page works." if ok else "No captcha solved within 5 minutes.")


def _login_time(fire_at: datetime | None, text: str) -> datetime | None:
    """The early-login time on the morning of ``fire_at`` (``off`` disables it)."""
    if not fire_at or text.lower() in ("off", "none", "no"):
        return None
    login_at = datetime.combine(fire_at.date(), _parse_hhmm(text), TZ)
    if login_at > fire_at - timedelta(minutes=5):
        raise SystemExit("--login-at must be at least 5 minutes before --at")
    return login_at


def _booking_choices(
    day: str,
    times: list[str] | None,
    courts: list[str] | None,
    any_court: bool,
) -> tuple[list[tuple[str, str | None]], list[int] | None, config.BookingPrefs]:
    prefs = config.booking_prefs(Date.fromisoformat(day))
    parsed_times = [parse_time_pref(t) for t in times] if times else prefs.times
    if any_court:
        parsed_courts = None
    elif courts:
        parsed_courts = [parse_court_pref(c) for c in courts]
    else:
        parsed_courts = prefs.courts
    return parsed_times, parsed_courts, prefs


def _print_target(
    venue: Venue,
    plan: Plan,
    pay: PayMethod,
    prefs: config.BookingPrefs,
    defaulted: list[str],
) -> None:
    when = ", ".join(f"{begin}" + (f"-{end}" if end else "") for begin, end in plan.times)
    court_list = ", ".join(f"{n:02d}" for n in plan.courts) if plan.courts else "any"
    kind = "weekend" if Date.fromisoformat(plan.date).weekday() >= 5 else "weekday"
    origin = f"  ({' and '.join(defaulted)} from {prefs.source})" if defaulted else ""
    print(f"Target: {venue.name} on {plan.date} ({Date.fromisoformat(plan.date):%a}, {kind} schedule); "
          f"times [{when}]; courts [{court_list}]; pay {pay.value}"
          + ("  [DRY RUN]" if plan.dry_run else "") + origin)


def _run_booking(
    client: VenueClient,
    plan: Plan,
    *,
    open_browser: bool,
    auto_captcha: bool,
) -> Result | None:
    desk = None
    if not plan.dry_run:
        desk = CaptchaDesk(client, open_browser=open_browser, autosolve=auto_captcha)
        url = desk.start()
        print(f"Captcha page: {url}" +
              ("" if auto_captcha else "  (keep it open — you'll solve the slider there)"))
    booker = Booker(client, desk, plan)
    # If the login lapses mid-run: try silently, else pop a window (and an alert).
    client.relogin = lambda: auto_login(
        client, interactive=True, timeout=110, log=booker.log, notify=booker.alert,
    )
    if desk:
        desk.notify = booker.notify
    try:
        return booker.run()
    finally:
        if desk:
            desk.stop()


@app.command("book", help="wait for the 08:00 opening and book a court (or --now)")
def cmd_book(
    time: Annotated[list[str] | None, typer.Option(
        help="a time inside the session you want, or an exact session like 20:00-22:00; "
             "repeat in order of preference (default: [booking] times, else 12:00)",
    )] = None,
    court: Annotated[list[str] | None, typer.Option(
        help="court number (e.g. 5 or 羽05); repeat in order of preference "
             "(default: [booking] courts, else 4 then 5)",
    )] = None,
    any_court: Annotated[bool, typer.Option("--any-court", help="accept any court (ignores the court defaults)")] = False,
    date: Annotated[str, typer.Option(
        help="+N days after the booking day, or YYYY-MM-DD (default +3, the day that opens at 08:00)",
    )] = "+3",
    at: Annotated[str, typer.Option(
        help="Beijing time booking opens; the script waits for it (default 08:00)",
    )] = "08:00",
    now: Annotated[bool, typer.Option("--now", help="book right away instead of waiting (e.g. cancellations)")] = False,
    login_at: Annotated[str, typer.Option(
        help="log in again automatically at this time on the booking morning (default 07:30; 'off')",
    )] = "07:30",
    pay: Annotated[PayMethod, typer.Option(
        help="online: pay on the site within 60 min; offline: pay at the cashier ≥24h before",
    )] = PayMethod.online,
    venue: VenueOption = None,
    captcha_lead: CaptchaLead = 120,
    no_alarm: Annotated[bool, typer.Option(
        "--no-alarm",
        help="no ringing/speech when the captcha is needed (just a notification)",
    )] = False,
    fire_delay: Annotated[float, typer.Option(
        help="start requesting this long after opening (default 0.1)",
    )] = 0.1,
    give_up: Annotated[float, typer.Option(
        help="stop trying this many seconds after opening (default 180)",
    )] = 180,
    no_browser: NoBrowserOption = False,
    auto_captcha: Annotated[bool, typer.Option(
        "--auto-captcha",
        help="solve the slider captcha automatically (computer vision); the alarm rings only if it's unsure",
    )] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="do everything except the captcha and the booking")] = False,
) -> None:
    c = _client()
    venue = _resolve_venue(venue)
    fire_at = None if now else _next_occurrence(at)
    login_at = _login_time(fire_at, login_at)
    day = _resolve_date(date, (fire_at or datetime.now(TZ)).date())
    times, courts, prefs = _booking_choices(day, time, court, any_court)
    plan = Plan(
        venue=venue,
        date=day,
        times=times,
        courts=courts,
        pay_type="PAY_OFFLINE" if pay == PayMethod.offline else "PAY_ONLINE",
        fire_at=fire_at,
        login_at=login_at,
        captcha_lead=captcha_lead if fire_at else 0,
        fire_delay=fire_delay,
        give_up_after=give_up,
        dry_run=dry_run,
        alarm=bool(fire_at) and not no_alarm,
    )
    defaulted = [name for name, given in (("times", time), ("courts", court or any_court))
                 if not given]
    _print_target(venue, plan, pay, prefs, defaulted)
    if fire_at:
        _print_schedule(plan, auto_captcha)
    try:
        result = _run_booking(c, plan, open_browser=not no_browser, auto_captcha=auto_captcha)
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
    sys.exit(0 if result else 1)


def _print_schedule(plan: Plan, auto_captcha: bool) -> None:
    fire = plan.fire_at
    day = "today" if fire.date() == datetime.now(TZ).date() else f"{fire:%a %m-%d}"
    rows = [("now", "check the login works (rehearses the automatic SSO login if needed)")]
    if plan.login_at and plan.login_at > datetime.now(TZ):
        rows.append((f"{plan.login_at:%H:%M}", "log in again automatically (no window)"))
    captcha_at = fire - timedelta(seconds=plan.captcha_lead)
    if plan.dry_run:
        rows.append((f"{captcha_at:%H:%M:%S}", "[dry-run] skip the captcha"))
    elif auto_captcha:
        rows.append((f"{captcha_at:%H:%M:%S}", "auto-solve the captcha (alarm + you only if unsure)"))
    else:
        rows.append((f"{captcha_at:%H:%M:%S}", "ALARM + captcha page → drag the slider once"
                     if plan.alarm else "captcha page → drag the slider once"))
    rows.append((f"{fire:%H:%M:%S}", f"book {plan.date} (first match from your list)"))
    print(f"\nPlan ({day}):")
    for when, what in rows:
        print(f"  {when:>9}  {what}")
    print("Keep the Mac on power with the lid open, and the volume up. Ctrl-C cancels.\n")


# ------------------------------------------------------------------ main
@app.callback()
def _validate_before_command(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand == "init-config":
        return
    config.validate()  # surface config-file mistakes now, not at 07:58
    if config.site().is_placeholder:
        raise config.ConfigError(
            "no booking site configured yet — add [site] and [venues] to your config "
            "(run `venue-booking init-config`, then see config.example.toml / the README)")


def main(argv: list[str] | None = None) -> None:
    try:
        app(args=argv, prog_name="venue-booking")
    except config.ConfigError as e:
        raise SystemExit(f"Config problem: {e}")
    except AuthError as e:
        raise SystemExit(f"Login problem: {e}")
    except ApiError as e:
        raise SystemExit(f"Site error: {e}")
