import pytest

from slots import parse_court_pref, parse_slots, parse_time_pref, rank_candidates
from fakes import FULL, NOT_OPEN, site


def test_parse_slots_reads_sessions():
    slots = parse_slots([site(1, {"19:00": FULL}), site(2, {})], "2026-11-03")
    assert len(slots) == 6
    s = next(s for s in slots if s.court == "羽01" and s.begin == "19:00")
    assert (s.bookable, s.reason, s.session_uuid, s.form_uuid) == (False, "当前场次预约人数已满", "sess-1-19:00", "form-1")
    assert s.label() == "2026-11-03 19:00-20:00 羽01"


@pytest.mark.parametrize("text,expected", [
    ("19", ("19:00", None)), ("9:30", ("09:30", None)), ("20:00-22:00", ("20:00", "22:00")),
])
def test_parse_time_pref(text, expected):
    assert parse_time_pref(text) == expected


@pytest.mark.parametrize("text,expected", [("5", 5), ("05", 5), ("羽12", 12)])
def test_parse_court_pref(text, expected):
    assert parse_court_pref(text) == expected


def test_rank_time_first_then_court_preference():
    slots = parse_slots([site(1, {"19:00": FULL}), site(3, {}), site(7, {})], "d")
    ranked = rank_candidates(slots, [parse_time_pref("19:00"), parse_time_pref("20:00")], [7, 1, 3])
    assert [(s.begin, s.court) for s in ranked] == [
        ("19:00", "羽07"), ("19:00", "羽03"),                    # 羽01 is taken at 19:00
        ("20:00", "羽07"), ("20:00", "羽01"), ("20:00", "羽03"),
    ]


def test_rank_any_court_sorted_by_number_and_end_time_filter():
    slots = parse_slots([site(10, {}), site(2, {})], "d")
    ranked = rank_candidates(slots, [("20:00", "22:00"), ("20:00", "21:00")], None)
    assert [s.court for s in ranked] == ["羽02", "羽10"]


def test_rank_not_open_only_when_asked():
    slots = parse_slots([site(1, {"19:00": NOT_OPEN})], "d")
    assert rank_candidates(slots, [("19:00", None)], None) == []
    assert len(rank_candidates(slots, [("19:00", None)], None, include_not_open=True)) == 1
