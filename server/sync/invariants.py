# SPDX-License-Identifier: GPL-3.0-or-later
"""
The invariants of the activity model (the app's docs/ACTIVITY-MODEL.md §1–3)
that a write must not break, issue #55. Each rule has a token, the detail of
the `invalid` error a refused change gets (docs/SYNC.md §5).

Two kinds of rules:
- of one record, checked on each change (record_rule): the parent's kind never
  changes, so a segment can be judged alone;
- across an activity and its segments (broken): legitimate commands pass
  through states that break them for a moment (a switch is three changes), so
  they are judged once the whole push, or the whole web command, is applied.

Data already stored may break a rule (migrated data): a change is refused only
when it creates the violation, not when it leaves an old one as it was.
"""

from records.models import ActivityKind, Entry, EntryStatus, Segment

KIND_CHANGED = "kind_changed"
ABSENCE_NOT_CLOSED = "absence_not_closed"
SEGMENT_KIND = "segment_kind"
PLANNED_NOT_TODO = "planned_not_todo"
MODE_MISMATCH = "mode_mismatch"
OPEN_SEGMENTS = "open_segments"
CLOSED_OPEN_SEGMENT = "closed_open_segment"
TODO_REAL_SEGMENTS = "todo_real_segments"

# the order a refusal names the first of several rules broken
ORDER = [KIND_CHANGED, ABSENCE_NOT_CLOSED, SEGMENT_KIND, PLANNED_NOT_TODO, OPEN_SEGMENTS, CLOSED_OPEN_SEGMENT, TODO_REAL_SEGMENTS,
         MODE_MISMATCH]


def before(rec, new):
    """What record_rule compares with: the fields it watches as stored before the change."""
    if new:
        return None
    if isinstance(rec, Entry):
        return {"kind": rec.kind, "status": rec.status}
    if isinstance(rec, Segment):
        return {"kind": rec.kind, "entry_id": rec.entry_id, "planned": rec.planned}
    return None


def record_rule(rec, old):
    """The token of the rule [rec] breaks as changed from [old] (None: new), else None."""
    if isinstance(rec, Entry):
        if old is not None and rec.kind != old["kind"]:
            # 1.1: the kind never changes after creation
            return KIND_CHANGED
        if rec.kind == ActivityKind.ABSENCE and rec.status != EntryStatus.CLOSED and (old is None or rec.status != old["status"]):
            # 2.1: an absence is always CLOSED
            return ABSENCE_NOT_CLOSED
    elif isinstance(rec, Segment):
        moved = old is None or rec.entry_id != old["entry_id"]
        if moved or rec.kind != old["kind"]:
            # 1.2: ABSENCE segments in absences only, and only those there
            if (rec.kind == "ABSENCE") != (rec.entry.kind == ActivityKind.ABSENCE):
                return SEGMENT_KIND
        if rec.planned and (moved or not old["planned"]) and rec.entry.status != EntryStatus.TODO:
            # 1.2: a plan belongs to a TODO activity. No legitimate command adds one
            # to an activity that is not TODO (the start or tick that leaves TODO
            # deletes them), so this one is refused at once.
            return PLANNED_NOT_TODO
    return None


def broken(entry_id):
    """The cross-record rules the activity [entry_id] breaks as stored now (a tombstone breaks none)."""
    e = Entry.objects.filter(pk=entry_id, deleted=False).values("status", "mode").first()
    if e is None:
        return frozenset()
    segs = list(Segment.objects.filter(entry_id=entry_id, deleted=False).values_list("kind", "end_utc", "planned"))
    out = set()
    if e["status"] != EntryStatus.TODO and any(planned for _, _, planned in segs):
        # 1.2: the start or tick that leaves TODO deletes the plan
        out.add(PLANNED_NOT_TODO)
    open_kinds = [kind for kind, end, planned in segs if end is None and not planned]
    if e["status"] == EntryStatus.CLOSED and open_kinds:
        # 3: stop closes the open segment (an ABSENCE one is never open); reopen opens one with RUNNING
        out.add(CLOSED_OPEN_SEGMENT)
    if e["status"] == EntryStatus.TODO and any(not planned for _, _, planned in segs):
        # 1.2, 2: a TODO has planned segments only; start makes it RUNNING, untick needs no real segment
        out.add(TODO_REAL_SEGMENTS)
    if len(open_kinds) > 1:
        # 3: at most one open segment
        out.add(OPEN_SEGMENTS)
    elif e["mode"] != (open_kinds[0] if open_kinds else None):
        # 1.1: mode is the open segment's kind, null when none is open
        out.add(MODE_MISMATCH)
    return frozenset(out)


def first(rules):
    return min(rules, key=ORDER.index)
