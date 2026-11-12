# SPDX-License-Identifier: GPL-3.0-or-later
"""
The app's report rules (core/domain report/ and rounding/): an entry's time
by calendar day, rounded per user, for the agenda and the report.

The phone's own settings do not travel (docs/ARCHITECTURE.md §9): the rules
are the ones the user's policy sets, else the app's defaults.
"""

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from django.db.models import Q
from django.utils import timezone

from records.models import Segment

CEIL_TOLERANT, CEIL, FLOOR = "CEIL_TOLERANT", "CEIL", "FLOOR"


@dataclass(frozen=True)
class Rule:
    mode: str
    quantum: int  # minutes
    minimum: int = 0

    def apply(self, seconds):
        """Rounded minutes of a duration in seconds (Rounding.kt)."""
        if seconds <= 0:
            return 0
        q = self.quantum * 60
        r = seconds % q
        base = seconds - r
        if self.mode == CEIL_TOLERANT:
            out = q if seconds <= q else seconds if r == 0 else base if 3 * r < q else base + q
        elif self.mode == CEIL:
            out = seconds if r == 0 else base + q
        else:
            out = base
        return max(out // 60, self.minimum)


@dataclass(frozen=True)
class Rules:
    work: Rule = Rule(CEIL_TOLERANT, 15)
    travel: Rule = Rule(CEIL, 5)
    pause: Rule = Rule(FLOOR, 5, 5)
    carry_over_min: int = 6 * 60


QUANTA = (1, 5, 15, 30, 60)
MODES = (CEIL_TOLERANT, CEIL, FLOOR)


def rules_for(user):
    """The rules of the user's policy settings (set, locked or not), else the app's defaults."""
    from policies.effective import merged

    s = merged(user).get("settings", {})

    def value(key, ok):
        v = s.get(key)
        v = v.get("value") if isinstance(v, dict) else v
        return v if v is not None and ok(v) else None

    def rule(name, default):
        quantum = value(f"rounding{name}Min", lambda v: v in QUANTA)
        mode = value(f"rounding{name}Mode", lambda v: v in MODES)
        minimum = value(f"rounding{name}MinimumMin", lambda v: isinstance(v, int) and 0 <= v <= 60)
        return Rule(mode or default.mode, quantum or default.quantum, default.minimum if minimum is None else minimum)

    d = Rules()
    carry = value("carryOverMin", lambda v: isinstance(v, int) and 0 <= v <= 720)
    return Rules(rule("Work", d.work), rule("Travel", d.travel), rule("Pause", d.pause), d.carry_over_min if carry is None else carry)


@dataclass
class Line:
    """One entry on one day (EntryDayLine): real seconds, rounded minutes."""
    entry: object
    day: object
    start: datetime
    end: datetime
    work: int = 0
    travel: int = 0
    pause: int = 0
    r_work: int = 0
    r_travel: int = 0
    r_pause: int = 0
    running: bool = False

    @property
    def reported(self):
        return self.r_work + self.r_travel


@dataclass
class AbsenceLine:
    """One absence on one day (ACTIVITY-MODEL §3.1): counted apart from work, travel and pause."""
    entry: object
    day: object
    start: datetime
    end: datetime
    seconds: int = 0


@dataclass
class Day:
    day: object
    lines: list = field(default_factory=list)
    absences: list = field(default_factory=list)

    @property
    def reported(self):
        """Work and travel, rounded; absences are not in it."""
        return sum(x.reported for x in self.lines)

    @property
    def absent(self):
        """Seconds of absence."""
        return sum(x.seconds for x in self.absences)


def _split(start, end, tz):
    """[start, end) cut at every local midnight: (day, start, end)."""
    out = []
    while start < end:
        day = timezone.localtime(start, tz).date()
        nxt = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
        part = min(nxt, end)
        out.append((day, start, part))
        start = part
    return out


def attribute(segments, rules, now, tz):
    """
    (day, kind, start, end) of an entry's segments (DayAttribution.kt): all
    on the start day when the entry ends by the carry-over time of the next
    day, else split at local midnights.
    """
    spans = [(s.kind, s.start_utc, s.end_utc or now) for s in segments]
    spans = [x for x in spans if x[2] > x[1]]
    if not spans:
        return []
    first = min(x[1] for x in spans)
    last = max(x[2] for x in spans)
    start_day = timezone.localtime(first, tz).date()
    limit = datetime.combine(start_day + timedelta(days=1), time.min, tzinfo=tz) + timedelta(minutes=rules.carry_over_min)
    if last <= limit:
        return [(start_day, k, a, b) for k, a, b in sorted(spans, key=lambda x: x[1])]
    return [(d, k, a2, b2) for k, a, b in sorted(spans, key=lambda x: x[1]) for d, a2, b2 in _split(a, b, tz)]


def lines(entry, segments, rules, now, tz):
    by_day = {}
    for day, kind, a, b in attribute(segments, rules, now, tz):
        by_day.setdefault(day, []).append((kind, a, b))
    running = any(s.end_utc is None for s in segments)
    out = []
    for day, parts in sorted(by_day.items()):
        secs = {"WORK": 0, "TRAVEL": 0}
        for kind, a, b in parts:
            if kind in secs:
                secs[kind] += int((b - a).total_seconds())
        first, last = min(p[1] for p in parts), max(p[2] for p in parts)
        pause = int((last - first).total_seconds()) - secs["WORK"] - secs["TRAVEL"]
        out.append(Line(
            entry, day, first, last, secs["WORK"], secs["TRAVEL"], max(pause, 0),
            rules.work.apply(secs["WORK"]), rules.travel.apply(secs["TRAVEL"]), rules.pause.apply(max(pause, 0)),
            running,
        ))
    return out


def report(entries, since, until, now=None):
    """
    The days since..until (dates) with the lines of the given entries (a
    queryset), each rounded with its owner's rules. Planned segments never
    count; the absences of the period are in each day's `absences`, apart
    from work, travel and pause (their lines and totals).
    """
    absent = entries.filter(kind="ABSENCE")
    entries = entries.filter(kind="ACTIVITY")
    tz = timezone.get_current_timezone()
    now = now or timezone.now()
    lo = datetime.combine(since, time.min, tzinfo=tz)
    hi = datetime.combine(until + timedelta(days=1), time.min, tzinfo=tz)
    # entries with time in the period, or the day before (carry-over)
    near = Segment.objects.filter(entry__in=entries, deleted=False, planned=False, start_utc__lt=hi).filter(
        Q(end_utc__isnull=True) | Q(end_utc__gt=lo - timedelta(days=1))).values("entry_id")
    picked = list(entries.filter(uid__in=near))
    segs = {}
    for s in Segment.objects.filter(entry__in=picked, deleted=False, planned=False).order_by("start_utc"):
        segs.setdefault(s.entry_id, []).append(s)
    rules = {}
    days = {}
    for e in picked:
        r = rules.get(e.owner_id) or rules.setdefault(e.owner_id, rules_for(e.owner))
        for line in lines(e, segs.get(e.uid, []), r, now, tz):
            if since <= line.day <= until:
                days.setdefault(line.day, Day(line.day)).lines.append(line)
    for line in absence_lines(absent, since, until, tz):
        days.setdefault(line.day, Day(line.day)).absences.append(line)
    for d in days.values():
        d.lines.sort(key=lambda x: x.start)
        d.absences.sort(key=lambda x: x.start)
    return days


def absence_lines(absent, since, until, tz):
    """The ABSENCE segments of [absent] (a queryset) per entry and local day in since..until."""
    lo = datetime.combine(since, time.min, tzinfo=tz)
    hi = datetime.combine(until + timedelta(days=1), time.min, tzinfo=tz)
    segs = Segment.objects.filter(
        entry__in=absent, deleted=False, planned=False, kind="ABSENCE", end_utc__isnull=False,
        start_utc__lt=hi, end_utc__gt=lo)
    segs = segs.select_related("entry__company", "entry__owner").order_by("start_utc")
    by = {}
    for sg in segs:
        for day, a, b in _split(sg.start_utc, sg.end_utc, tz):
            if since <= day <= until:
                line = by.get((sg.entry_id, day))
                if line is None:
                    line = by[(sg.entry_id, day)] = AbsenceLine(sg.entry, day, a, b)
                line.start, line.end = min(line.start, a), max(line.end, b)
                line.seconds += int((b - a).total_seconds())
    return list(by.values())


def hm(minutes):
    return f"{minutes // 60}:{minutes % 60:02d}"


def hm_seconds(seconds):
    return hm(seconds // 60)
