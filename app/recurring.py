"""Recurring tasks: turning a rhythm into one task at a time.

`logic.py` owns the date arithmetic — given a rule and where the rhythm has
got to, when is the next one. This module owns what the app does with that
answer: which task gets copied, when the copy appears, and what happens to the
series when you finish, drop, or delete the thing on your list.

The rule the whole module is built around is **one open occurrence at a time**.
A daily chore you ignored for a fortnight must not come back as fourteen
identical overdue rows — that is the pile this app exists to prevent, and it is
also a lie: you are not fourteen bin-days behind, you are one. So the sweep
never creates a second copy while the first is still open, and a series whose
date has come round again while you were not looking quietly moves to the next
one instead of stacking.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import db, logic

log = logging.getLogger(__name__)

# A sweep that has been away for months still only ever produces one task per
# series, but the loop that walks the rhythm forward to catch up is bounded so
# a pathological rule can never hold the request open.
CATCH_UP_STEPS = 500
# Fields copied from one occurrence to the next. Deliberately not: status,
# actual_time, started_at, xp_awarded, the deadline or the start time — every
# one of those is about the occurrence you did, not about the job that comes
# back. A rhythm that happens at a particular hour says so in its rule (the
# `time` field), which is where the next copy's date and hour both come from.
TEMPLATE_FIELDS = ("title", "description", "estimated_time", "impact", "effort")


def _tz(settings: dict | None = None):
    return logic.resolve_tz((settings or db.get_settings()).get("timezone"))


def _iso(when: datetime | None) -> str | None:
    return when.isoformat(timespec="seconds") if when else None


# ---------- the template: what a copy is a copy of ----------

def snapshot(task_id: str) -> dict:
    """A picture of a task and its steps, as the thing to copy next time.

    Taken from the live task rather than from whatever was typed when the
    repeat was set up, so editing this week's copy — a better title, a truer
    estimate, one more step — is how you edit the series. Discarded steps are
    left out: dropping a step from a routine should stick, or dropping it
    means nothing.

    Statuses are not part of the picture at all, which is what lets the
    snapshot be taken at any moment, including after the whole tree has just
    been marked done.
    """
    task = db.get_task(task_id)
    if task is None:
        return {}
    children: dict[str | None, list[dict]] = {}
    for row in db.list_tasks(task["project_id"]):
        children.setdefault(row["parent_id"], []).append(row)
    for sibs in children.values():
        sibs.sort(key=lambda t: (t["order_index"], t["created_at"]))

    def picture(row: dict) -> dict:
        return {
            **{f: row[f] for f in TEMPLATE_FIELDS},
            "collapsed": bool(row["collapsed"]),
            "subtasks": [picture(kid) for kid in children.get(row["id"], [])
                         if kid["status"] != "discarded"],
        }

    return picture(task)


def _plant(template: dict, project_id: str, parent_id: str | None,
           deadline: str | None, series_id: str | None) -> dict:
    """Write a template back out as real tasks, deepest steps and all.

    Only the top of the tree carries the deadline and the series link: the
    steps underneath are backward-scheduled from it by `logic.compute`, the
    same as any hand-made subtask, and the series owns the occurrence rather
    than each of its parts.
    """
    task = db.create_task({
        **{f: template.get(f) for f in TEMPLATE_FIELDS},
        "description": template.get("description") or "",
        "collapsed": bool(template.get("collapsed")),
        "project_id": project_id,
        "parent_id": parent_id,
        "deadline": deadline,
        "series_id": series_id,
    })
    for kid in template.get("subtasks") or []:
        _plant(kid, project_id, task["id"], None, None)
    return task


# ---------- starting, changing and stopping a rhythm ----------

def _end_of_working_day(now: datetime, settings: dict, tz) -> datetime:
    """Today, at the hour the working day closes — in local time.

    Where an undated task's first occurrence lands when the rule names no time
    of day. "Take the bins out, every 3 days" set at two in the morning must
    not arrive already overdue by forty seconds, and it does not mean two in
    the morning either: a chore with no time on it is due by the end of the
    day, which is a number this app already keeps (the working window, from
    `day_start` for as long as a day holds).
    """
    window = logic.day_planner(settings).window_end
    minute = min(window, 24 * 60 - 1)
    local = now.astimezone(tz)
    return (datetime.combine(local.date(), datetime.min.time(), tzinfo=tz)
            + timedelta(minutes=minute))


def first_occurrence(rule: dict, task: dict | None = None,
                     settings: dict | None = None,
                     now: datetime | None = None) -> datetime | None:
    """When a rhythm's first beat falls, given the task it is being set on.

    A task that already has a deadline keeps it — that date is the first beat.
    One without gets the first date the rule matches from now, found *without*
    the interval: asking for "every two weeks on a Tuesday" on a Thursday
    should give you next Tuesday and then a fortnight, not make you wait two
    and a half weeks for the series to start.
    """
    rule = logic.normalize_rule(rule)
    if not rule:
        return None
    settings = settings or db.get_settings()
    tz = _tz(settings)
    now = now or datetime.now(timezone.utc)
    if task and task["status"] in logic.ACTIVE_STATUSES:
        # A finished task's date is behind you; a repeat set from the Done list
        # means "from now on", not "from whenever that one was due".
        existing = logic.parse_dt(task["deadline"])
        if existing:
            return existing
    # Occurrences land on whole minutes, so "the first one from now" is probed
    # from the start of the current minute rather than from `now` itself —
    # otherwise today's beat is 40 seconds in the past and "every day", set at
    # noon, would quietly mean tomorrow.
    probe = now.replace(second=0, microsecond=0) - timedelta(seconds=1)
    # The anchor supplies the clock time when the rule names none, so an
    # undated task gets the end of the working day rather than this exact
    # second — and every occurrence after it keeps that hour.
    seed = now if rule["time"] else _end_of_working_day(now, settings or {}, tz)
    return logic.next_occurrence({**rule, "interval": 1}, probe, anchor=seed, tz=tz)


def upcoming(rule: dict, anchor: datetime, limit: int = 5,
             settings: dict | None = None) -> list[datetime]:
    """The next few dates a rule produces, starting at its first beat.

    What the repeat dialog shows underneath the controls. Three or four real
    dates say what a rule means far better than the rule does — "the last
    Friday of every second month" is a sentence; *27 Feb, 24 Apr, 26 Jun* is
    an answer.
    """
    rule = logic.normalize_rule(rule)
    if not rule or anchor is None:
        return []
    tz = _tz(settings)
    out = [anchor]
    cursor = anchor
    while len(out) < max(1, limit):
        if rule["count"] is not None and len(out) >= rule["count"]:
            break
        cursor = logic.next_occurrence(rule, cursor, anchor=anchor, tz=tz)
        if cursor is None:
            break
        out.append(cursor)
    return out


def start_series(task: dict, rule: dict, settings: dict | None = None,
                 now: datetime | None = None) -> dict | None:
    """Make an existing task the first occurrence of a repeating job.

    `first_occurrence` decides where the rhythm starts; from there it is
    phased on that beat and stays phased on it, so "every two weeks" means the
    same two weeks forever however many copies come and go. A task with no
    deadline is given that first beat as its own, which is the whole point of
    saying "every Tuesday" about something undated.
    """
    rule = logic.normalize_rule(rule)
    if not rule:
        return None
    settings = settings or db.get_settings()
    tz = _tz(settings)
    now = now or datetime.now(timezone.utc)

    anchor = first_occurrence(rule, task, settings, now)
    if anchor is None:
        return None

    following = logic.next_occurrence(rule, anchor, anchor=anchor, tz=tz)
    if rule["count"] is not None and rule["count"] <= 1:
        following = None  # "once" is not a rhythm; the task in hand is all of it

    series = db.create_series(
        project_id=task["project_id"], rule=rule, template={},
        anchor_at=_iso(anchor), next_at=_iso(following), made=1,
    )
    fields = {"series_id": series["id"]}
    if not task["deadline"]:
        fields["deadline"] = _iso(anchor)
    db.update_task(task["id"], fields)
    db.update_series(series["id"], {"template": snapshot(task["id"])})
    return db.get_series(series["id"])


def change_rule(series: dict, rule: dict, task: dict | None = None,
                settings: dict | None = None,
                now: datetime | None = None) -> dict | None:
    """Re-time a running series onto a new rule, keeping its history.

    The occurrence in front of you does not move — you may be halfway through
    it — but everything after it is re-planned from its date, so "actually,
    make that every other week" takes effect from the next one.
    """
    rule = logic.normalize_rule(rule)
    if not rule:
        return None
    settings = settings or db.get_settings()
    tz = _tz(settings)
    now = now or datetime.now(timezone.utc)
    anchor = logic.parse_dt(series["anchor_at"]) or now
    if task is not None:
        anchor = logic.parse_dt(task["deadline"]) or anchor
    after = max(anchor, now) if rule["from_completion"] else anchor
    following = logic.next_occurrence(rule, after, anchor=anchor, tz=tz)
    if rule["count"] is not None and series["made"] >= rule["count"]:
        following = None
    return db.update_series(series["id"], {
        "rule": rule, "anchor_at": _iso(anchor), "next_at": _iso(following),
        "active": following is not None,
    })


def stop_series(series_id: str) -> None:
    """Stop a job coming back, on purpose. The rule is forgotten outright.

    Whatever is on the list stays on the list — it is a real task you may
    still mean to do; it just has no sequel — and the occurrences behind it
    become ordinary finished tasks. Deleting the row rather than flagging it
    off is what makes "stop repeating" mean it: nothing lingers to describe a
    rhythm that no longer exists.

    A series that simply runs out (its count spent, its end date passed) is
    left in place and marked inactive instead, so the copy in your hand can
    still say what it was the last of.
    """
    db.delete_series(series_id)


def _exhaust(series_id: str) -> None:
    """A rhythm that has run its course: kept, but over.

    Unlike `stop_series` this leaves the row behind, so the last copy on your
    list can still say what it was the last of — "every day · 3 times" is
    worth reading while you do the third.
    """
    db.update_series(series_id, {"active": False, "next_at": None})


# ---------- closing one occurrence, opening the next ----------

def close_occurrence(task: dict, closed_at: datetime | None = None,
                     settings: dict | None = None) -> dict | None:
    """Called when an occurrence is finished, dropped, or deleted.

    Three things happen, in this order: the series takes a fresh picture of
    the task (so this week's edits are next week's starting point), the rhythm
    steps past the occurrence just closed, and — if the next one is close
    enough to be worth seeing — the copy is made there and then rather than
    waiting for the nightly sweep. Finishing Monday's chore on Monday evening
    should put Tuesday's on the list before you close the laptop.
    """
    series = db.get_series(task.get("series_id"))
    if not series or not series["active"]:
        return None
    settings = settings or db.get_settings()
    tz = _tz(settings)
    closed_at = closed_at or datetime.now(timezone.utc)
    rule = logic.normalize_rule(series["rule"])
    if not rule:
        _exhaust(series["id"])
        return None

    fields: dict = {"template": snapshot(task["id"]) or series["template"]}
    anchor = logic.parse_dt(series["anchor_at"])

    if rule["from_completion"]:
        # "Three days after I finish", not "three days after it was due" —
        # so the rhythm re-phases on the moment you actually finished.
        anchor = closed_at
        following = logic.next_occurrence(rule, closed_at, anchor=closed_at, tz=tz)
        fields["anchor_at"] = _iso(closed_at)
    else:
        following = logic.parse_dt(series["next_at"])
        # An occurrence closed long after its date leaves the schedule behind
        # it: step forward to the first one still ahead, so clearing a backlog
        # hands you the next chore rather than last month's.
        steps = 0
        while following is not None and following <= closed_at and steps < CATCH_UP_STEPS:
            following = logic.next_occurrence(rule, following, anchor=anchor, tz=tz)
            steps += 1

    fields["next_at"] = _iso(following)
    if following is None:
        fields["active"] = False
    series = db.update_series(series["id"], fields)
    if series and series["active"]:
        materialize(series, now=closed_at, settings=settings)
    return db.get_series(series["id"])


def materialize(series: dict, now: datetime | None = None,
                settings: dict | None = None) -> dict | None:
    """Put the next copy on the list, if it is time and there is room for it.

    "Room" means the list has no open copy of this job already: that single
    check is what keeps a neglected daily chore at one row instead of thirty.
    When the rhythm has come round again while the last copy is still sitting
    there, the series steps forward without creating anything — you are one
    bin-day behind, not fourteen.
    """
    now = now or datetime.now(timezone.utc)
    settings = settings or db.get_settings()
    tz = _tz(settings)
    series = db.get_series(series["id"]) or series
    if not series["active"]:
        return None
    rule = logic.normalize_rule(series["rule"])
    due = logic.parse_dt(series["next_at"])
    if not rule or due is None:
        _exhaust(series["id"])
        return None
    if rule["count"] is not None and series["made"] >= rule["count"]:
        _exhaust(series["id"])
        return None
    if due > now + timedelta(days=rule["lead_days"]):
        return None  # real, just not yet: the sweep will come back to it

    anchor = logic.parse_dt(series["anchor_at"]) or due
    following = logic.next_occurrence(rule, due, anchor=anchor, tz=tz)

    if db.has_open_occurrence(series["id"]):
        # Still one on the list. Skip this beat rather than duplicating it,
        # and leave the rhythm pointing at a date that is actually ahead.
        steps = 0
        while following is not None and following <= now and steps < CATCH_UP_STEPS:
            following = logic.next_occurrence(rule, following, anchor=anchor, tz=tz)
            steps += 1
        db.update_series(series["id"], {"next_at": _iso(following),
                                        "active": following is not None})
        return None

    project = db.get_project(series["project_id"]) or db.ensure_project()
    template = series["template"] or {"title": "Recurring task"}
    task = _plant(template, project["id"], None, _iso(due), series["id"])

    made = series["made"] + 1
    if rule["count"] is not None and made >= rule["count"]:
        following = None
    db.update_series(series["id"], {
        "last_at": _iso(due), "next_at": _iso(following), "made": made,
        "project_id": project["id"], "active": following is not None,
    })
    log.info("recurring: created %s (%s) for series %s, due %s",
             task["id"], task["title"], series["id"], _iso(due))
    return task


# ---------- the daily job ----------

def sweep(now: datetime | None = None) -> dict:
    """One pass of the scheduled job: everything due, made real.

    Idempotent by construction — a series is only ever advanced past the
    occurrence it actually produced — so running it twice a minute, once a
    day, or after a week of downtime all reach the same place. That matters
    more than it sounds: this runs on a laptop that sleeps, and a job that has
    to fire at midnight to work would simply never work.
    """
    now = now or datetime.now(timezone.utc)
    settings = db.get_settings()
    cutoff = _iso(now + timedelta(days=logic.RECUR_MAX_LEAD_DAYS))
    created: list[dict] = []
    for series in db.due_series(cutoff):
        try:
            task = materialize(series, now=now, settings=settings)
        except Exception:  # one bad rule must not stall every other rhythm
            log.exception("recurring: series %s failed to materialize", series["id"])
            continue
        if task:
            created.append(task)
    if created:
        log.info("recurring: sweep created %d task(s)", len(created))
    return {"ran_at": _iso(now), "created": created}


# ---------- what the page is told ----------

def describe(series: dict | None, settings: dict | None = None) -> dict | None:
    """The series as the front end needs it: the rule, in words, and when the
    next one lands. The sentence is built here so the badge on the task, the
    repeat dialog and the calendar cannot drift apart."""
    if not series:
        return None
    tz = _tz(settings)
    anchor = logic.parse_dt(series["anchor_at"])
    rule = logic.normalize_rule(series["rule"]) or {}
    return {
        "series_id": series["id"],
        "rule": rule,
        "summary": logic.describe_rule(rule, anchor, tz),
        "anchor_at": series["anchor_at"],
        "next_at": series["next_at"],
        "made": series["made"],
        "active": series["active"],
    }


def by_task(tasks: list[dict], settings: dict | None = None) -> dict[str, dict]:
    """{task_id: describe(series)} for whichever of `tasks` repeat."""
    settings = settings or db.get_settings()
    cache: dict[str, dict | None] = {}
    out: dict[str, dict] = {}
    for task in tasks:
        sid = task.get("series_id")
        if not sid:
            continue
        if sid not in cache:
            cache[sid] = describe(db.get_series(sid), settings)
        if cache[sid]:
            out[task["id"]] = cache[sid]
    return out
