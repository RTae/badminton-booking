"""Weekend vs weekday schedules and the 12:00 / courts 4-5 defaults."""

from datetime import date

import pytest

import config
from slots import parse_slots, parse_time_pref, rank_candidates

SAT, MON = date(2026, 11, 7), date(2026, 11, 9)

# The real schedules (seen on the site 2026-10-31).
WEEKEND = [("07:00", "08:00"), ("08:00", "10:00"), ("10:00", "12:00"), ("12:00", "14:00"),
           ("14:00", "16:00"), ("16:00", "18:00"), ("18:00", "20:00"), ("20:00", "22:00")]
WEEKDAY = [("07:00", "08:00"), ("08:00", "09:00"), ("09:00", "11:30"), ("11:30", "13:00"),
           ("13:00", "15:00"), ("15:00", "16:00"), ("16:00", "17:00"), ("17:00", "18:00"),
           ("18:00", "19:00"), ("19:00", "20:00"), ("20:00", "22:00")]


def courts(schedule, numbers=(3, 4, 5, 6), taken=()):
    return [{
        "uuid": f"site-{n}", "sceneUuid": "scene", "siteName": f"羽{n:02d}",
        "formRuleVo": {"formUuid": "form"},
        "sessionVo": [{"uuid": f"s{n}-{b}", "beginTime": b, "endTime": e,
                       "reserveStatus": {"reserveStatus": "N" if (n, b) in taken else "Y",
                                         "reserveStatusReason": "当前场次预约人数已满" if (n, b) in taken else ""}}
                      for b, e in schedule],
    } for n in numbers]


def ranked(schedule, times, court_prefs, **kw):
    slots = parse_slots(courts(schedule, **kw), "d")
    return [(s.begin, s.end, s.court) for s in rank_candidates(slots, [parse_time_pref(t) for t in times], court_prefs)]


# ------------------------------------------------------------ time matching
def test_noon_is_the_two_hour_block_on_weekends():
    assert ranked(WEEKEND, ["12:00"], [4, 5]) == [("12:00", "14:00", "羽04"), ("12:00", "14:00", "羽05")]


def test_noon_is_the_1130_session_on_weekdays():
    assert ranked(WEEKDAY, ["12:00"], [4, 5]) == [("11:30", "13:00", "羽04"), ("11:30", "13:00", "羽05")]


def test_a_session_start_time_still_matches_exactly():
    assert ranked(WEEKDAY, ["19:00"], [4])[0][:2] == ("19:00", "20:00")
    # 19:00 on a weekend falls inside 18:00-20:00
    assert ranked(WEEKEND, ["19:00"], [4])[0][:2] == ("18:00", "20:00")


def test_session_end_is_not_inside_it():
    # 12:00 must not pick 10:00-12:00 on weekends even if 12:00-14:00 is full.
    assert ranked(WEEKEND, ["12:00"], [4], taken={(4, "12:00")}) == []


def test_explicit_range_stays_exact():
    assert ranked(WEEKDAY, ["12:00-14:00"], [4]) == []
    assert ranked(WEEKEND, ["12:00-14:00"], [4]) == [("12:00", "14:00", "羽04")]


def test_court_order_then_next_time():
    got = ranked(WEEKEND, ["12:00", "14:00"], [5, 4], taken={(5, "12:00")})
    assert got == [("12:00", "14:00", "羽04"), ("14:00", "16:00", "羽05"), ("14:00", "16:00", "羽04")]


# ----------------------------------------------------------------- defaults
@pytest.fixture
def cfg(monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    monkeypatch.setenv("VB_CONFIG", str(path))
    config._warned.clear()

    def write(text):
        path.write_text(text)
        path.chmod(0o600)
    write("")
    return write


def test_builtin_default_is_noon_on_court_4_then_5(cfg):
    for day in (SAT, MON):
        p = config.booking_prefs(day)
        assert p.times == [("12:00", None)] and p.courts == [4, 5] and p.source == "built-in default"


def test_config_booking_section_overrides(cfg):
    cfg("[booking]\ntimes = ['14:00', '12:00']\ncourts = ['羽06', 3]\n")
    p = config.booking_prefs(SAT)
    assert p.times == [("14:00", None), ("12:00", None)] and p.courts == [6, 3] and p.source == "[booking]"


def test_weekday_section_only_applies_mon_to_fri(cfg):
    cfg("[booking]\ntimes = ['12:00']\ncourts = [4, 5]\n[booking.weekday]\ntimes = ['19:00']\ncourts = []\n")
    sat, mon = config.booking_prefs(SAT), config.booking_prefs(MON)
    assert sat.times == [("12:00", None)] and sat.courts == [4, 5]
    assert mon.times == [("19:00", None)] and mon.courts is None and mon.source == "[booking.weekday]"


def test_partial_override_mixes_sources(cfg):
    cfg("[booking.weekend]\ncourts = [7]\n")
    p = config.booking_prefs(SAT)
    assert p.times == [("12:00", None)] and p.courts == [7]
    assert "built-in" in p.source and "[booking.weekend]" in p.source


@pytest.mark.parametrize("text,match", [
    ("[booking]\ntimes = []\n", "empty"),
    ("[booking]\ntimes = ['noon']\n", "can't read"),
    ("[booking]\ncourts = {a = 1}\n", "must be a list"),
    ("booking = 3\n", "must be a table"),
])
def test_bad_booking_config_is_reported(cfg, text, match):
    cfg(text)
    with pytest.raises(config.ConfigError, match=match):
        config.validate()


