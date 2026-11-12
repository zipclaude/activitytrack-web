# SPDX-License-Identifier: GPL-3.0-or-later
"""
The Home list as in the app (ui/home/HomeTabs.kt homeSections, manual §3):
activities to do (the TODO ones) and closed ones in one list with the
filters period, state, company and tag.
"""

from datetime import datetime, timedelta

from django.utils import timezone

from records import activity as A

# period: -1 today, 0 everything, else hours
WINDOWS = (-1, 24, 72, 168, 720, 0)
TODO, MISSING, DONE = "todo", "missing", "done"
SHOWS = (TODO, MISSING, DONE)
DEFAULT_SHOW = (TODO, MISSING)
SECTIONS = ("overdue", "missing", "today", "next", "earlier", "no_date")


def _at(day, minute, tz):
    return A.at_minute(day, minute, tz)


def plan_bounds(t, tz):
    """(start, end, all day) of an activity's plan (TodoPlan.kt), or None; duration is elapsed."""
    return A.plan_bounds(t, tz)


def moment(t, tz):
    """The plan's start, else the due moment (todoMoment)."""
    plan = plan_bounds(t, tz)
    if plan:
        return plan[0]
    if t.due_date:
        return _at(t.due_date, min(max(t.due_time_min or 0, 0), 1440), tz)
    return None


def today_from(carry_over_min, now, tz):
    """Today begins at the carry-over time (06:00), as in the reports; before it, yesterday's (closedBoundary)."""
    today = timezone.localtime(now, tz).date()
    start = _at(today, carry_over_min, tz)
    return start if start <= now else _at(today - timedelta(days=1), carry_over_min, tz)


def sections(todos, stopped, show, window, company=None, tag=None, carry_over_min=360, now=None):
    """
    [(section, [item])], empty sections left out. todos: the TODO activities
    of the user; stopped: [(entry, end)] of the closed ones; both annotated
    with their displayed state (web.states.annotate, ACTIVITY-MODEL §2.2).
    An item is ("todo", activity, state) or ("entry", entry, end): a closed
    one not complete (INCOMPLETE) is a "details missing".
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

    if TODO in show and not tag:
        for t in todos:
            if not ours(t.company_id):
                continue
            m = moment(t, tz)
            if t.shown == A.LATE:
                groups["overdue"].append(((m or far).timestamp(), ("todo", t, t.shown)))
            elif m is None:
                # the most urgent first (5 emergency .. 1 low), then none
                key = (-(t.priority or 0), (t.activity or "").lower())
                groups["no_date"].append((key, ("todo", t, t.shown)))
            elif ahead is not None and m > ahead:
                continue
            elif timezone.localtime(m, tz).date() > today:
                groups["next"].append((m.timestamp(), ("todo", t, t.shown)))
            else:
                groups["today"].append((("0", m.timestamp()), ("todo", t, t.shown)))
    for e, end in stopped:
        if not ours(e.company_id) or (tag and tag not in (e.tags or [])):
            continue
        if e.shown == A.INCOMPLETE:
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
