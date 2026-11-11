"""Local config file: the venue site to talk to, your SSO login, booking defaults.

Looked up in this order (first one that exists wins):

1. the path in ``$VB_CONFIG``
2. ``./config.toml`` in the directory you run the command from (git-ignored)
3. ``~/.config/venue-booking/config.toml``

The site-specific details (base URL, SSO host, app id, signing key and the
venue ids) live in the ``[site]`` and ``[venues]`` sections of this local file,
not in the source, so the checked-out code carries no particular institution's
identifiers. Fill them in from a browser capture of your own booking session
(see ``config.example.toml`` and the README).

Example::

    [site]
    base_url  = 'https://booking.example.edu'
    sso_host  = 'id.example.edu'
    app_id    = '0000000000000000000'
    sign_key  = '00000000000000000000000000000000'

    [venues.main]
    name          = 'Main hall'
    scene_uuid    = '...'
    dev_kind_uuid = '...'
    class_type_uuid = '...'

    [sso]
    username = '2025xxxxxx'
    password = 'your password'

    [booking]              # used when `book` runs without --time / --court
    times = ['12:00']      # in order of preference
    courts = [4, 5]        # in order of preference; [] = any court

The password is stored in plain text, so the file should be readable only by
you (``chmod 600``). ``venue-booking init-config`` creates it that way, and we
print a warning if other users can read it.
"""

from __future__ import annotations

import os
import stat
import sys
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, TypeVar

from slots import parse_court_pref, parse_time_pref

APP_NAME = "venue-booking"
CONFIG_DIR = Path.home() / ".config" / APP_NAME
USER_CONFIG = CONFIG_DIR / "config.toml"
LOCAL_CONFIG = Path("config.toml")

# Generic stand-ins used when the config has no [site] / [venues] section yet.
# They let the code and tests run; real values come from your local config.
PLACEHOLDER_BASE = "https://booking.example.invalid"
PLACEHOLDER_SSO = "id.example.invalid"
PLACEHOLDER_APP_ID = "0000000000000000000"
PLACEHOLDER_KEY = "0" * 32

TEMPLATE = """\
# venue-booking settings — keep this file private (it holds your password).
# Use single quotes: then special characters in the password need no escaping.
# (If your password contains a single quote ', use double quotes and write \\" for " and \\\\ for \\.)

# The booking site. Capture these from your own browser session (see README /
# config.example.toml): the host you book on, the SSO sign-in host, and the
# appId + signing key the site's requests use.
[site]
base_url = ''      # e.g. 'https://booking.example.edu'
sso_host = ''      # e.g. 'id.example.edu'
app_id = ''
sign_key = ''
# site_path = '/venue/site'          # API base appended to base_url (default)
# index_path = '/venue/index.html'   # the booking page (default)

# One or more bookable venues. Copy the ids from a 'reserve/current/page'
# request in your capture. The first one is the default for --venue.
[venues.main]
name = ''
scene_uuid = ''
dev_kind_uuid = ''
class_type_uuid = ''
# scene_use_type = 'SPORT_GROUP'   # defaults
# class_type_enum = 'ROOM'

[sso]
username = ''   # student / staff ID, e.g. '2025xxxxxx'
password = ''

# What `book` books when you don't pass --time / --court.
# A time picks the session containing it: '12:00' is 12:00-14:00 on weekends
# and 11:30-13:00 on weekdays.
[booking]
times = ['12:00']   # in order of preference
courts = [4, 5]     # in order of preference; [] = any court

# Optional: other choices for weekdays (Mon-Fri) or weekends (Sat/Sun).
# [booking.weekday]
# times = ['19:00', '20:00']
# courts = []
"""

DEFAULT_TIMES = ["12:00"]
DEFAULT_COURTS = [4, 5]
T = TypeVar("T")


class ConfigError(ValueError):
    pass


# ----------------------------------------------------------------- site
@dataclass(frozen=True)
class Site:
    base_url: str
    sso_host: str
    app_id: str
    sign_key: str
    site_path: str = "/venue/site"
    index_path: str = "/venue/index.html"

    @property
    def origin(self) -> str:
        return self.base_url

    @property
    def site_base(self) -> str:
        return self.base_url + self.site_path

    @property
    def index_url(self) -> str:
        return self.base_url + self.index_path

    @property
    def is_placeholder(self) -> bool:
        return not self.base_url or self.base_url == PLACEHOLDER_BASE


@dataclass
class Venue:
    """Request parameters that identify one venue's court list."""

    name: str
    scene_uuid: str
    dev_kind_uuid: str
    class_type_uuid: str
    scene_use_type: str = "SPORT_GROUP"
    class_type_enum: str = "ROOM"


PLACEHOLDER_SITE = Site(PLACEHOLDER_BASE, PLACEHOLDER_SSO, PLACEHOLDER_APP_ID, PLACEHOLDER_KEY)
PLACEHOLDER_VENUES = {"main": Venue("Main hall", "scene-uuid", "devkind-uuid", "classtype-uuid")}


def config_path() -> Path | None:
    env = os.environ.get("VB_CONFIG")
    if env:
        return Path(env).expanduser()
    for candidate in (LOCAL_CONFIG, USER_CONFIG):
        if candidate.is_file():
            return candidate
    return None


def load() -> dict[str, Any]:
    path = config_path()
    if path is None:
        return {}
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file {path} (from $VB_CONFIG) does not exist")
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} is not valid TOML: {e}")
    _warn_if_readable_by_others(path)
    return data


def site() -> Site:
    """The booking site from ``[site]``, or a generic placeholder if unset."""
    return _site(load())


def _site(data: dict[str, Any]) -> Site:
    raw = data.get("site") or {}
    if not isinstance(raw, dict):
        raise ConfigError("[site] in the config file must be a table")
    if not raw.get("base_url"):
        return PLACEHOLDER_SITE
    try:
        return Site(
            base_url=str(raw["base_url"]).rstrip("/"),
            sso_host=str(raw.get("sso_host") or ""),
            app_id=str(raw.get("app_id") or ""),
            sign_key=str(raw.get("sign_key") or ""),
            site_path=str(raw.get("site_path") or "/venue/site"),
            index_path=str(raw.get("index_path") or "/venue/index.html"),
        )
    except (KeyError, TypeError) as e:
        raise ConfigError(f"[site]: {e}")


def venues() -> dict[str, Venue]:
    """The venues from ``[venues]``, or a generic placeholder if unset."""
    return _venues(load())


def _venues(data: dict[str, Any]) -> dict[str, Venue]:
    raw = data.get("venues") or {}
    if not isinstance(raw, dict):
        raise ConfigError("[venues] in the config file must be a table")
    out: dict[str, Venue] = {}
    for key, v in raw.items():
        if not isinstance(v, dict):
            raise ConfigError(f"[venues.{key}] must be a table")
        missing = [f for f in ("name", "scene_uuid", "dev_kind_uuid", "class_type_uuid") if not v.get(f)]
        if missing:
            raise ConfigError(f"[venues.{key}] is missing: {', '.join(missing)}")
        out[key] = Venue(
            name=str(v["name"]),
            scene_uuid=str(v["scene_uuid"]),
            dev_kind_uuid=str(v["dev_kind_uuid"]),
            class_type_uuid=str(v["class_type_uuid"]),
            scene_use_type=str(v.get("scene_use_type") or "SPORT_GROUP"),
            class_type_enum=str(v.get("class_type_enum") or "ROOM"),
        )
    return out or dict(PLACEHOLDER_VENUES)


def default_venue_key() -> str:
    return next(iter(venues()))


# -------------------------------------------------------------- booking
@dataclass
class BookingPrefs:
    times: list[tuple[str, str | None]]
    courts: list[int] | None          # None = any court
    source: str                       # where they came from, for display


def booking_prefs(day: date) -> BookingPrefs:
    """Default times/courts for ``day``: [booking.weekend|weekday], then [booking], then built-in."""
    return _booking_prefs(load(), day)


def _booking_prefs(data: dict[str, Any], day: date) -> BookingPrefs:
    booking = data.get("booking") or {}
    if not isinstance(booking, dict):
        raise ConfigError("[booking] in the config file must be a table")
    kind = "weekend" if day.weekday() >= 5 else "weekday"
    specific = booking.get(kind) or {}
    if not isinstance(specific, dict):
        raise ConfigError(f"[booking.{kind}] in the config file must be a table")

    def pick(key: str, default):
        if key in specific:
            return specific[key], f"[booking.{kind}]"
        if key in booking:
            return booking[key], "[booking]"
        return default, "built-in default"

    raw_times, times_src = pick("times", DEFAULT_TIMES)
    raw_courts, courts_src = pick("courts", DEFAULT_COURTS)
    times = [_parse(parse_time_pref, t, "times") for t in _as_list(raw_times, "times")]
    if not times:
        raise ConfigError(f"{times_src} times is empty — list at least one time, e.g. ['12:00']")
    courts = [_parse(parse_court_pref, c, "courts") for c in _as_list(raw_courts, "courts")]
    source = times_src if times_src == courts_src else f"times: {times_src}, courts: {courts_src}"
    return BookingPrefs(times=times, courts=courts or None, source=source)


def _as_list(value: object, key: str) -> list[object]:
    if isinstance(value, (str, int)):
        return [value]
    if isinstance(value, list):
        return value
    example = "[4, 5]" if key == "courts" else "['12:00']"
    raise ConfigError(f"[booking] {key} must be a list, e.g. {key} = {example}")


def _parse(fn: Callable[[str], T], value: object, key: str) -> T:
    try:
        return fn(str(value))
    except (ValueError, IndexError):
        raise ConfigError(f"[booking] {key}: can't read {value!r}")


# -------------------------------------------------------------------- sso
def sso_credentials() -> tuple[str, str] | None:
    """``(username, password)`` from the config file, or ``None`` if not set."""
    return _sso_credentials(load())


def _sso_credentials(data: dict[str, Any]) -> tuple[str, str] | None:
    sso = data.get("sso") or {}
    if not isinstance(sso, dict):
        raise ConfigError("[sso] in the config file must be a table")
    user = str(sso.get("username") or "").strip()
    password = str(sso.get("password") or "")
    if bool(user) != bool(password):
        raise ConfigError(f"{config_path()}: set both [sso] username and password (one is empty)")
    return (user, password) if user else None


def validate() -> None:
    """Parse everything once at startup so mistakes surface immediately."""
    data = load()
    _sso_credentials(data)
    _site(data)
    _venues(data)
    _booking_prefs(data, date(2026, 1, 3))  # a Saturday
    _booking_prefs(data, date(2026, 1, 5))  # a Monday


# ------------------------------------------------------------------ file
_warned: set[Path] = set()


def _warn_if_readable_by_others(path: Path) -> None:
    resolved = path.resolve()
    if resolved in _warned:
        return
    mode = resolved.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        _warned.add(resolved)
        print(f"warning: {path} holds your password but other users can read it — "
              f"run: chmod 600 {path}", file=sys.stderr)


def create(path: Path) -> Path:
    """Write the template with owner-only permissions. Refuses to overwrite."""
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # fails if it exists
    with os.fdopen(fd, "w") as f:
        f.write(TEMPLATE)
    return path
