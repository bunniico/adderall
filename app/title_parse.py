"""Reading a deadline and a repeat rule straight off a task's own title.

"Pay rent tomorrow" should not need a date picker afterwards, and "take the
bins out every day" should not need the repeat dialog either — the words are
already there. This is the fast, local half of that: a fixed vocabulary of
English phrasing, matched with plain regexes, at effectively no cost. It is
deliberately conservative — anything it does not recognise it leaves alone,
title and all — because a false match that quietly reassigns a deadline is
far worse than one it fails to catch. `ai.extract_schedule` is the fallback
for phrasing loose enough that this misses it.

Recurrence is looked for before deadline, on the same text, so "every monday"
is never misread as the one-off deadline "monday" once the recurrence phrase
around it has already been claimed.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone

from . import logic

WEEKDAYS = {
    "monday": 0, "mon": 0, "mondays": 0,
    "tuesday": 1, "tue": 1, "tues": 1, "tuesdays": 1,
    "wednesday": 2, "wed": 2, "weds": 2, "wednesdays": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "thursdays": 3,
    "friday": 4, "fri": 4, "fridays": 4,
    "saturday": 5, "sat": 5, "saturdays": 5,
    "sunday": 6, "sun": 6, "sundays": 6,
}
# Longest names first, so "wednesday" matches whole rather than stopping at
# a shorter prefix another key happens to share.
_WEEKDAY_ALTS = "|".join(sorted(WEEKDAYS, key=len, reverse=True))


def _weekday_list(blob: str) -> list[int]:
    days: list[int] = []
    for token in re.split(r",|&|\band\b", blob, flags=re.I):
        token = token.strip().lower()
        wd = WEEKDAYS.get(token)
        if wd is not None and wd not in days:
            days.append(wd)
    return sorted(days)


def _next_weekday(today: date, target: int, *, strictly_next_week: bool) -> date:
    delta = (target - today.weekday()) % 7
    if strictly_next_week and delta == 0:
        delta = 7
    return today + timedelta(days=delta)


# ---------------- recurrence ----------------
# Order matters: the specific ("every other day", "every 3 weeks") has to be
# tried before the generic ("daily", "weekly") it would otherwise also match.

RECUR_PATTERNS: list[tuple[re.Pattern, "callable"]] = [
    (re.compile(r"\bevery\s+other\s+day\b", re.I),
     lambda m: {"freq": "daily", "interval": 2}),
    (re.compile(r"\bevery\s+(\d+)\s+days?\b", re.I),
     lambda m: {"freq": "daily", "interval": int(m.group(1))}),
    (re.compile(r"\bevery\s*day\b", re.I),
     lambda m: {"freq": "daily", "interval": 1}),

    (re.compile(r"\bevery\s+other\s+week\b", re.I),
     lambda m: {"freq": "weekly", "interval": 2}),
    (re.compile(r"\bevery\s+(\d+)\s+weeks?\b", re.I),
     lambda m: {"freq": "weekly", "interval": int(m.group(1))}),
    (re.compile(rf"\bevery\s+((?:{_WEEKDAY_ALTS})(?:\s*(?:,|&|and)\s*(?:{_WEEKDAY_ALTS}))*)\b", re.I),
     lambda m: {"freq": "weekly", "interval": 1, "weekdays": _weekday_list(m.group(1))}),
    (re.compile(r"\bevery\s*week\b", re.I),
     lambda m: {"freq": "weekly", "interval": 1}),

    (re.compile(r"\bevery\s+other\s+month\b", re.I),
     lambda m: {"freq": "monthly", "interval": 2}),
    (re.compile(r"\bevery\s+(\d+)\s+months?\b", re.I),
     lambda m: {"freq": "monthly", "interval": int(m.group(1))}),
    (re.compile(r"\bevery\s*month\b", re.I),
     lambda m: {"freq": "monthly", "interval": 1}),

    (re.compile(r"\bevery\s+(\d+)\s+years?\b", re.I),
     lambda m: {"freq": "yearly", "interval": int(m.group(1))}),
    (re.compile(r"\bevery\s*year\b", re.I),
     lambda m: {"freq": "yearly", "interval": 1}),
]

# ---------------- one-off deadline ----------------
# A leading connector ("by", "due", "on") is folded into the match so it is
# stripped along with the date word rather than left dangling. Bare weekday
# names ("monday") only count as a deadline when one of those connectors
# says so explicitly — otherwise a task that simply mentions a day by name
# would get a deadline nobody asked for.

_CONNECTOR = r"(?:\b(?:due|by|on)\s+)?"

DEADLINE_PATTERNS: list[tuple[re.Pattern, "callable"]] = [
    (re.compile(_CONNECTOR + r"\bin\s+(\d+)\s+weeks?\b", re.I),
     lambda m, today: today + timedelta(weeks=int(m.group(1)))),
    (re.compile(_CONNECTOR + r"\bin\s+(\d+)\s+days?\b", re.I),
     lambda m, today: today + timedelta(days=int(m.group(1)))),
    (re.compile(_CONNECTOR + r"\btomorrow\b", re.I),
     lambda m, today: today + timedelta(days=1)),
    (re.compile(_CONNECTOR + r"\b(?:today|tonight)\b", re.I),
     lambda m, today: today),
    (re.compile(_CONNECTOR + rf"\bnext\s+({_WEEKDAY_ALTS})\b", re.I),
     lambda m, today: _next_weekday(today, WEEKDAYS[m.group(1).lower()],
                                    strictly_next_week=True)),
    (re.compile(_CONNECTOR + rf"\bthis\s+({_WEEKDAY_ALTS})\b", re.I),
     lambda m, today: _next_weekday(today, WEEKDAYS[m.group(1).lower()],
                                    strictly_next_week=False)),
    (re.compile(rf"\b(?:due|by|on)\s+({_WEEKDAY_ALTS})\b", re.I),
     lambda m, today: _next_weekday(today, WEEKDAYS[m.group(1).lower()],
                                    strictly_next_week=False)),
]

# "at 5pm" / "at 17:30" / "at 9" (only when unambiguous — see below).
_TIME_RE = re.compile(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.I)


def _extract_time(text: str) -> tuple[tuple[int, int] | None, str]:
    m = _TIME_RE.search(text)
    if not m:
        return None, text
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower()
    if not ampm and not (hour == 0 or hour >= 13):
        # "at 5" with no am/pm and no 24-hour cue is genuinely ambiguous —
        # guessing wrong is worse than not touching it at all.
        return None, text
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None, text
    return (hour, minute), text[:m.start()] + text[m.end():]


_WHITESPACE_RE = re.compile(r"\s{2,}")
_EDGE_PUNCT_RE = re.compile(r"^[\s,;:.\-–—]+|[\s,;:.\-–—]+$")


def _clean(text: str) -> str:
    text = _WHITESPACE_RE.sub(" ", text)
    return _EDGE_PUNCT_RE.sub("", text).strip()


def parse_title(title: str, now: datetime, settings: dict) -> dict:
    """Pull a deadline and/or repeat rule out of `title`, and the title with
    them removed.

    Returns `{"clean_title", "deadline", "repeat", "matched"}`. `deadline` is
    a ready-to-store UTC ISO instant or `None`; `repeat` is a raw rule dict
    for `logic.normalize_rule`, or `None`. When nothing is recognised,
    `matched` is `False` and `clean_title` is `title`, unchanged.
    """
    tz = logic.resolve_tz(settings.get("timezone"))
    today = now.astimezone(tz).date()

    time_of_day, text = _extract_time(title)

    repeat = None
    for rx, handler in RECUR_PATTERNS:
        m = rx.search(text)
        if m:
            repeat = handler(m)
            text = text[:m.start()] + text[m.end():]
            break

    deadline_date = None
    for rx, handler in DEADLINE_PATTERNS:
        m = rx.search(text)
        if m:
            deadline_date = handler(m, today)
            text = text[:m.start()] + text[m.end():]
            break

    if not repeat and deadline_date is None:
        return {"clean_title": title, "deadline": None, "repeat": None,
                "matched": False}

    if repeat is not None and time_of_day is not None:
        repeat["time"] = f"{time_of_day[0]:02d}:{time_of_day[1]:02d}"

    deadline = None
    if deadline_date is not None:
        if time_of_day is not None:
            hour, minute = time_of_day
        else:
            minute_of_day = min(logic.day_planner(settings).window_end, 24 * 60 - 1)
            hour, minute = divmod(minute_of_day, 60)
        local_dt = datetime.combine(deadline_date, time(hour, minute), tzinfo=tz)
        deadline = local_dt.astimezone(timezone.utc).isoformat(timespec="seconds")

    clean_title = _clean(text) or title.strip()
    return {"clean_title": clean_title, "deadline": deadline, "repeat": repeat,
            "matched": True}
