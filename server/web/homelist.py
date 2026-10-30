# SPDX-License-Identifier: GPL-3.0-or-later
"""
The Home list as in the app (ui/home/HomeTabs.kt homeSections, manual §3):
to-dos and stopped entries in one list with the filters period, state,
company and tag.
"""

from datetime import datetime, time, timedelta

from django.utils import timezone

# period: -1 today, 0 everything, else hours
WINDOWS = (-1, 24, 72, 168, 720, 0)
TODO, MISSING, DONE, IGNORED = "todo", "missing", "done", "ignored"
SHOWS = (TODO, MISSING, DONE, IGNORED)
DEFAULT_SHOW = (TODO, MISSING)
SECTIONS = ("overdue", "missing", "today", "next", "earlier", "no_date", "ignored")


def _at(day, minute, tz):
    return datetime.combine(day, time.min, tzinfo=tz) + timedelta(minutes=minute)


def plan_bounds(t, tz):
    """(start, end, all day) of a to-do's plan (TodoPlan.kt), or None."""
    if not t.plan_date:
        return None
    if t.plan_start_min is None:
        start = _at(t.plan_date, 0, tz)
        return start, start + timedelta(days=1), True
    start = _at(t.plan_date, t.plan_start_min, tz)
    return start, start + timedelta(minutes=t.duration_min or 0), False


def moment(t, tz):
    """The plan's start, else the due moment (todoMoment)."""
    plan = plan_bounds(t, tz)
    if plan:
        return plan[0]
    if t.due_date:
        return _at(t.due_date, min(max(t.due_time_min or 0, 0), 1440), tz)
    return None


def overdue(t, now, tz):
    if t.done or t.ignored_utc:
        return False
    plan = plan_bounds(t, tz)
    if plan and (now >= plan[1] if plan[2] else now > plan[1]):
        return True
    if t.due_date:
        limit = _at(t.due_date, min(max(t.due_time_min, 0), 1440), tz) if t.due_time_min is not None else _at(t.due_date + timedelta(days=1), 0, tz)
        return now > limit
    return False


def state(t, now, tz):
    """done, ignored, overdue, planned or open (todoState)."""
    if t.done:
        return "done"
    if t.ignored_utc:
        return "ignored"
    if overdue(t, now, tz):
        return "overdue"
    return "planned" if t.plan_date else "open"


def today_from(carry_over_min, now, tz):
    """Today begins at the carry-over time (06:00), as in the reports; before it, yesterday's (closedBoundary)."""
    today = timezone.localtime(now, tz).date()
    start = _at(today, carry_over_min, tz)
    return start if start <= now else _at(today - timedelta(days=1), carry_over_min, tz)


def sections(todos, stopped, show, window, company=None, tag=None, carry_over_min=360, now=None, working=frozenset()):
    """
    [(section, [item])], empty sections left out. todos: every to-do of the
    user; stopped: [(entry, end)] of the closed and details-missing entries;
    working: uids of to-dos with an activity still open, shown by it.
    An item is ("todo", to-do, state) or ("entry", entry, end).
    """
    tz = timezone.get_current_timezone()
    now = now or timezone.now()
    today = timezone.localtime(now, tz).date()
    start_today = today_from(carry_over_min, now, tz)
    ahead = _at(today + timedelta(days=1), 0, tz) if window == -1 else None if window == 0 else now + timedelta(hours=window)
    behind = start_today if window == -1 else None if window == 0 else now - timedelta(hours=window)
    groups = {s: [] for s in SECTIONS}
    far = datetime.max.replace(tzinfo=tz)

    def ours(company_id):
        return company is None or company_id == company

    with_todos = not tag
    for t in todos:
        if not ours(t.company_id) or not with_todos:
            continue
        st = state(t, now, tz)
        if st in ("done", "ignored"):
            at = (t.done_utc or t.updated_utc) if st == "done" else t.ignored_utc
            if st == "done" and DONE in show and (behind is None or at >= behind):
                key = ("1", -at.timestamp()) if at >= start_today else -at.timestamp()
                groups["today" if at >= start_today else "earlier"].append((key, ("todo", t, st)))
            elif st == "ignored" and IGNORED in show and (behind is None or at >= behind):
                groups["ignored"].append(((-at.timestamp(),), ("todo", t, st)))
            continue
        if TODO not in show or t.uid in working:
            continue
        m = moment(t, tz)
        if st == "overdue":
            groups["overdue"].append(((m or far).timestamp(), ("todo", t, st)))
        elif m is None:
            groups["no_date"].append(((t.priority or 99, t.title.lower()), ("todo", t, st)))
        elif ahead is not None and m > ahead:
            continue
        elif timezone.localtime(m, tz).date() > today:
            groups["next"].append((m.timestamp(), ("todo", t, st)))
        else:
            groups["today"].append((("0", m.timestamp()), ("todo", t, st)))
    for e, end in stopped:
        if not ours(e.company_id) or (tag and tag not in (e.tags or [])):
            continue
        if e.status == "TO_COMPLETE":
            if MISSING in show:
                groups["missing"].append((-end.timestamp(), ("entry", e, end)))
        elif DONE in show and (behind is None or end >= behind):
            # after today's to-dos, the most recent first
            key = ("1", -end.timestamp()) if end >= start_today else -end.timestamp()
            groups["today" if end >= start_today else "earlier"].append((key, ("entry", e, end)))
    out = []
    for s in SECTIONS:
        rows = groups[s]
        if rows:
            rows.sort(key=lambda r: r[0])
            out.append((s, [r[1] for r in rows]))
    return out
