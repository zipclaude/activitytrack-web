# SPDX-License-Identifier: GPL-3.0-or-later
"""The activity model 1.0 against the cases shared with the app (tests/data, see its README)."""

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase

from records import activity as A
from records.activity_migration import migrate

DATA = Path(__file__).parent / "data"


def _dt(text):
    return datetime.fromisoformat(text) if text else None


def _iso(dt):
    return dt.isoformat() if dt else None


class DisplayTests(SimpleTestCase):
    """display() of ACTIVITY-MODEL §2.2-2.4, every case of activity-states.json."""

    def test_the_shared_cases(self):
        doc = json.loads((DATA / "activity-states.json").read_text())
        zone, now = ZoneInfo(doc["zone"]), _dt(doc["now"])
        for case in doc["cases"]:
            a = case["activity"]
            plan, due = a.get("plan") or {}, a.get("due") or {}
            activity = SimpleNamespace(
                kind=a["kind"], status=a["status"], deleted_utc=_dt(a.get("deletedAt")), closed_utc=_dt(a.get("closedAt")),
                client_id="c" if a.get("client") else None,
                plan_date=date.fromisoformat(plan["day"]) if plan else None, plan_start_min=plan.get("startMin"),
                duration_min=plan.get("durationMin"),
                due_date=date.fromisoformat(due["day"]) if due else None, due_time_min=due.get("timeMin"),
            )
            segments = [SimpleNamespace(kind=s["kind"], start_utc=_dt(s["start"]), end_utc=_dt(s.get("end")), planned=s.get("planned", False))
                        for s in case["segments"]]
            with self.subTest(case["name"]):
                state, since = A.display(activity, segments, now, zone, case.get("missingRequired", False))
                self.assertEqual(state, case["expect"]["state"])
                expected = _dt(case["expect"]["since"])
                self.assertEqual(_iso(since.astimezone(zone)) if since else None, _iso(expected))

    def test_absence_segments(self):
        tz = ZoneInfo("Europe/Rome")
        schedule = [(3, 540, 780), (3, 780, 1020), (3, 600, 700), (4, 480, 720)]
        lunch = [(3, 750, 810)]
        # Wednesday 2026-10-28: windows merged (touching, inside), then the lunch break cut out; Thursday off
        out = A.absence_segments(date(2026, 10, 27), date(2026, 10, 29), tz, schedule=schedule, lunch=lunch, days_off={date(2026, 10, 29)})
        self.assertEqual([(d.isoformat(), n, a.strftime("%H:%M"), b.strftime("%H:%M")) for d, n, a, b in out],
                         [("2026-10-28", 0, "09:00", "12:30"), ("2026-10-28", 1, "13:30", "17:00")])
        # with times, a new absence keeps to the working days; the migration's permesso does not
        timed = A.absence_segments(date(2026, 10, 27), date(2026, 10, 29), tz, 900, 90, schedule, lunch, {date(2026, 10, 29)})
        self.assertEqual([(d.day, b - a) for d, _, a, b in timed], [(28, timedelta(minutes=90))])
        every = A.absence_segments(date(2026, 10, 27), date(2026, 10, 27), tz, 900, None, with_times_every_day=True)
        self.assertEqual([(d.day, a == b) for d, _, a, b in every], [(27, True)])
        # Sunday 2026-03-29, spring forward at 02:00: 02:00-03:00 and 02:30-03:00 are empty or inverted on the
        # real clock and give no segment, n counts the kept ones (python3-activitytrack#22)
        for gap in (120, 150):
            out = A.absence_segments(date(2026, 3, 29), date(2026, 3, 29), tz, schedule=[(7, 60, 300)],
                                     lunch=[(7, 90, gap), (7, 180, 240)])
            self.assertEqual([(n, a.strftime("%H:%M"), b.strftime("%H:%M")) for _, n, a, b in out],
                             [(0, "01:00", "01:30"), (1, "04:00", "05:00")])

    def test_priority_and_labels(self):
        self.assertEqual([A.priority_from_legacy(p) for p in (None, 1, 2, 3, 4, 5, 7, 0, -1)], [0, 5, 4, 3, 2, 1, 1, 0, 0])
        self.assertEqual([A.absence_kind_of(t) for t in (" ferie ", "SICK LEAVE", "Urlaub", "Permiso", "Dentista", "", None)],
                         ["HOLIDAY", "SICK", "HOLIDAY", "LEAVE", None, None, None])
        # the app's trim() and lowercase(): no-break spaces trimmed, U+0085 kept, no case folding (#22)
        self.assertEqual([A.absence_kind_of(t) for t in ("\u00a0Ferie\u3000", "Ferie\x85", "\u017fick leave", "A\u00dfenza")],
                         ["HOLIDAY", None, None, None])
        self.assertEqual(set(A._WS), {c for c in map(chr, range(0x110000)) if c.isspace()} - {"\x85"})


# ---- the migration cases: the app's tables (DB 21) to the server's rows and back to the cases' normal form ----

EPOCH = date(1970, 1, 1)
# what the server keeps of a time_entry and a segment (the app's other columns do not travel)
ENTRY_COLUMNS = ("uid", "kind", "status", "mode", "activity", "notes", "company_id", "client_id", "zone_id", "created_utc_ms",
                 "updated_utc_ms", "closed_utc_ms", "deleted_utc_ms", "external_key", "due_epoch_day", "due_time_min",
                 "plan_epoch_day", "plan_start_min", "duration_min", "priority", "absence_kind", "plugin_fields")
SEGMENT_COLUMNS = ("uid", "entry_uid", "kind", "start_utc_ms", "end_utc_ms", "zone_id", "source", "planned")
REF_COLUMNS = ("uid", "entry_uid", "kind", "value")
# the server's field -> the app's column
DAYS = {"due_date": "due_epoch_day", "plan_date": "plan_epoch_day", "plan_end_date": "plan_end_epoch_day"}
KEYS = {"company": "company_id", "client": "client_id"}


def _day(epoch_day):
    return None if epoch_day is None else (EPOCH + timedelta(days=epoch_day)).isoformat()


def _epoch_day(text):
    return None if text is None else (date.fromisoformat(text) - EPOCH).days


def _json(text):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return text


def to_server(before):
    """The cases' DB 21 tables as migrate() reads them (owner None: one person's phone)."""
    entry_uid = {e["id"]: e["uid"] for e in before.get("time_entry", [])}
    todo_uid = {t["id"]: t["uid"] for t in before.get("todo", [])}
    return {
        "entries": [{
            "uid": e["uid"], "owner": None, "company": e.get("company_id"), "client": e.get("client_id"), "notes": e.get("notes"),
            "status": e["status"], "mode": e.get("mode"), "todo": todo_uid.get(e.get("todo_id")), "activity": e.get("activity"),
            "plugin_fields": _json(e.get("plugin_fields")), "created_utc_ms": e["created_utc_ms"], "updated_utc_ms": e["updated_utc_ms"],
        } for e in before.get("time_entry", [])],
        "segments": [{
            "uid": s.get("uid") or "", "entry": entry_uid[s["entry_id"]], "kind": s["kind"], "start_utc_ms": s["start_utc_ms"],
            "end_utc_ms": s.get("end_utc_ms"),
        } for s in before.get("segment", [])],
        "external_refs": [{"uid": r.get("uid"), "entry": entry_uid[r["entry_id"]], "kind": r["kind"], "value": r["value"]}
                          for r in before.get("external_ref", [])],
        "todos": [{
            "uid": t["uid"], "owner": None, "kind": t.get("kind") or "TASK", "title": t.get("title"), "notes": t.get("notes"),
            "refs": t.get("refs") or "", "company": t.get("company_id"), "client": t.get("client_id"), "external_key": t.get("external_key"),
            "due_date": _day(t.get("due_epoch_day")), "due_time_min": t.get("due_time_min"), "priority": t.get("priority"),
            "done": bool(t.get("done")), "done_utc_ms": t.get("done_utc_ms"), "created_utc_ms": t["created_utc_ms"],
            "updated_utc_ms": t["updated_utc_ms"], "plan_date": _day(t.get("plan_epoch_day")), "plan_start_min": t.get("plan_start_min"),
            "duration_min": t.get("duration_min"), "ignored_utc_ms": t.get("ignored_utc_ms"), "plugin_fields": _json(t.get("plugin_fields")),
            "plan_end_date": _day(t.get("plan_end_epoch_day")),
        } for t in sorted(before.get("todo", []), key=lambda t: t["id"])],
        "absences": [{"uid": a["uid"], "owner": None, "date": _day(a["date_epoch_day"]), "kind": a["kind"], "time_min": a.get("time_min")}
                     for a in before.get("absence", [])],
        "work_schedules": [{"owner": None, **{k: w[k] for k in ("day_of_week", "start_min", "end_min")}} for w in before.get("work_schedule", [])],
        "lunch_breaks": [{"owner": None, **{k: w[k] for k in ("day_of_week", "start_min", "end_min")}} for w in before.get("lunch_break", [])],
        "days_off": [{"owner": None, "date": _day(d["date_epoch_day"])} for d in before.get("day_off", [])],
    }


def _app_entry(fields):
    out = {}
    for k, v in fields.items():
        if k in DAYS:
            out[DAYS[k]] = _epoch_day(v)
        else:
            out[KEYS.get(k, k)] = v
    return out


def normal(rows, columns):
    """The cases' normal form on the server's columns: nulls left out, booleans as 0/1, sorted."""
    out = []
    for r in rows:
        x = {k: (int(v) if isinstance(v, bool) else v) for k, v in r.items() if k in columns and v is not None}
        out.append(x)
    return sorted(out, key=lambda x: json.dumps(x, sort_keys=True))


def run_case(case):
    """(expected, got) of a case in normal form."""
    before = case["before"]
    out = migrate(to_server(before), case["zone"])
    entry_uid = {e["id"]: e["uid"] for e in before.get("time_entry", [])}
    entries = []
    for e in before.get("time_entry", []):
        row = {k: v for k, v in e.items() if k not in ("id", "todo_id")}
        row["plugin_fields"] = _json(row.get("plugin_fields"))
        row.update(_app_entry(out["entries"][e["uid"]]))
        entries.append(row)
    entries += [_app_entry({**f, "uid": uid}) for uid, f in out["entries"].items() if f.get("new")]
    segments = [{**{k: v for k, v in s.items() if k not in ("id", "entry_id")}, "entry_uid": entry_uid[s["entry_id"]], "planned": 0}
                for s in before.get("segment", [])]
    segments += [{**s, "entry_uid": s["entry"]} for s in out["segments"]]
    refs = [{**{k: v for k, v in r.items() if k not in ("id", "entry_id")}, "entry_uid": entry_uid[r["entry_id"]]}
            for r in before.get("external_ref", [])]
    refs += [{**r, "entry_uid": r["entry"]} for r in out["external_refs"]]
    after = case["after"]
    expected = (normal(after["time_entry"], ENTRY_COLUMNS), normal(after["segment"], SEGMENT_COLUMNS), normal(after["external_ref"], REF_COLUMNS))
    got = (normal(entries, ENTRY_COLUMNS), normal(segments, SEGMENT_COLUMNS), normal(refs, REF_COLUMNS))
    return expected, got


class MigrationCaseTests(SimpleTestCase):
    """records.activity_migration.migrate against every case of activity-migration.json."""

    maxDiff = None

    def test_the_shared_cases(self):
        doc = json.loads((DATA / "activity-migration.json").read_text())
        self.assertGreaterEqual(len(doc["cases"]), 34)
        for case in doc["cases"]:
            with self.subTest(case["name"]):
                expected, got = run_case(case)
                self.assertEqual(got, expected)

    def test_an_ignored_absence_goes_to_the_bin(self):
        """Where the server parts from the reference: an ignored absence is deleted, as an ignored task."""
        todo = {"id": 1, "uid": "a1", "kind": "ABSENCE", "title": "Ferie", "created_utc_ms": 1, "updated_utc_ms": 2,
                "plan_epoch_day": 20000, "ignored_utc_ms": 5}
        out = migrate(to_server({"todo": [todo]}), "Europe/Rome")
        self.assertEqual((out["entries"]["a1"]["deleted_utc_ms"], out["entries"]["a1"]["absence_kind"]), (5, "HOLIDAY"))

    def test_tombstones_and_clashing_keys(self):
        """Server only: tombstones count for nothing; a management-system key stays unique for each owner and company."""
        t = {"uid": "t", "owner": 1, "title": "x", "created_utc_ms": 1, "updated_utc_ms": 1, "external_key": "K", "company": "c"}
        data = {
            "todos": [t, {**t, "uid": "u", "created_utc_ms": 2}, {**t, "uid": "v", "owner": 2}, {**t, "uid": "gone", "deleted": True}],
            "entries": [{"uid": "e", "owner": 1, "status": "CLOSED", "todo": "gone", "created_utc_ms": 1, "updated_utc_ms": 1}],
            "segments": [{"uid": "s", "entry": "e", "kind": "WORK", "start_utc_ms": 1, "end_utc_ms": 9, "deleted": True}],
        }
        out = migrate(data, "Europe/Rome")["entries"]
        self.assertEqual(sorted(out), ["e", "t", "u", "v"])
        self.assertEqual([out[x]["external_key"] for x in ("t", "u", "v")], ["K", None, "K"])
        self.assertEqual(out["e"]["closed_utc_ms"], 1)

    def test_blank_keys_are_none(self):
        """A blank key is no key: it skips the uniqueness pass and must not reach the constraint of 0009 either."""
        t = {"uid": "t", "owner": 1, "title": "x", "created_utc_ms": 1, "updated_utc_ms": 1, "external_key": "", "company": "c"}
        out = migrate({"todos": [t, {**t, "uid": "u", "external_key": " "}, {**t, "uid": "v", "external_key": None}]},
                      "Europe/Rome")["entries"]
        self.assertEqual([out[x]["external_key"] for x in ("t", "u", "v")], [None, None, None])
