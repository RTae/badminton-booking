"""Turn the court-list response into bookable slots and rank them by preference."""

from __future__ import annotations

import re
from dataclasses import dataclass

# reserveStatusReason values seen in the wild.
REASON_NOT_OPEN = "未开放"          # not open for booking yet
REASON_FULL = "当前场次预约人数已满"  # already booked
REASON_ENDED = "场次已结束"         # session is already over (earlier today)


@dataclass(frozen=True)
class Slot:
    date: str            # YYYY-MM-DD
    court: str           # e.g. "羽05"
    site_uuid: str
    scene_uuid: str
    form_uuid: str
    session_uuid: str    # sessionDetailUuid
    begin: str           # HH:MM
    end: str             # HH:MM
    bookable: bool
    reason: str
    booked_by: tuple[str, ...] = ()   # user ids holding this court at this time

    @property
    def court_no(self) -> int | None:
        m = re.search(r"(\d+)", self.court)
        return int(m.group(1)) if m else None

    @property
    def maybe_bookable_later(self) -> bool:
        """True for slots that are closed only because booking hasn't opened yet."""
        return self.bookable or self.reason == REASON_NOT_OPEN

    def label(self) -> str:
        return f"{self.date} {self.begin}-{self.end} {self.court}"


def parse_slots(sites: list[dict], date: str) -> list[Slot]:
    slots: list[Slot] = []
    for site in sites:
        form_uuid = (site.get("formRuleVo") or {}).get("formUuid", "")
        holders: dict[str, list[str]] = {}
        for info in site.get("reserveInfo") or []:
            if info.get("userId"):
                holders.setdefault(info.get("startTime", ""), []).append(str(info["userId"]))
        for sess in site.get("sessionVo") or []:
            status_info = sess.get("reserveStatus") or {}
            slots.append(Slot(
                date=date,
                court=site.get("siteName", "?"),
                site_uuid=site["uuid"],
                scene_uuid=site.get("sceneUuid", ""),
                form_uuid=form_uuid,
                session_uuid=sess["uuid"],
                begin=sess["beginTime"],
                end=sess["endTime"],
                bookable=status_info.get("reserveStatus") == "Y",
                reason=status_info.get("reserveStatusReason", ""),
                booked_by=tuple(holders.get(sess["beginTime"], ())),
            ))
    return slots


def parse_time_pref(text: str) -> tuple[str, str | None]:
    """``"19"`` / ``"19:00"`` / ``"19:00-20:00"`` -> ``("19:00", "20:00" | None)``."""
    def norm(t: str) -> str:
        t = t.strip()
        if ":" not in t:
            t = f"{t}:00"
        h, m = t.split(":")
        return f"{int(h):02d}:{int(m):02d}"

    if "-" in text:
        begin, end = text.split("-", 1)
        return norm(begin), norm(end)
    return norm(text), None


def parse_court_pref(text: str) -> int:
    """``"5"`` / ``"05"`` / ``"羽05"`` -> ``5``."""
    m = re.search(r"(\d+)", text)
    if not m:
        raise ValueError(f"cannot read a court number from {text!r}")
    return int(m.group(1))


def rank_candidates(
    slots: list[Slot],
    times: list[tuple[str, str | None]],
    courts: list[int] | None,
    *,
    include_not_open: bool = False,
) -> list[Slot]:
    """Slots matching the preferences, best first.

    A bare time (``("12:00", None)``) matches the session that *contains* it,
    so one preference works with both schedules: weekends run 2-hour blocks
    (12:00-14:00) while weekdays have 11:30-13:00. A time with an end
    (``("11:30", "13:00")``) matches only that exact session.

    Order is time preference first, then court preference (or court number
    when any court is fine). With ``include_not_open`` we also keep slots that
    are only closed because booking hasn't opened yet.
    """
    def usable(s: Slot) -> bool:
        return s.maybe_bookable_later if include_not_open else s.bookable

    def matches_time(s: Slot, begin: str, end: str | None) -> bool:
        if end is not None:
            return s.begin == begin and s.end == end
        return s.begin <= begin < s.end

    ranked: list[Slot] = []
    seen: set[Slot] = set()
    order = {c: i for i, c in enumerate(courts)} if courts else {}
    for begin, end in times:
        matches = [s for s in slots if usable(s) and matches_time(s, begin, end)]
        if courts:
            matches = [s for s in matches if s.court_no in order]
            matches.sort(key=lambda s: order[s.court_no])
        else:
            matches.sort(key=lambda s: (s.court_no is None, s.court_no or 0, s.court))
        for match in matches:
            if match not in seen:
                seen.add(match)
                ranked.append(match)
    return ranked
