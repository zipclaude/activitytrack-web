# Activity model 1.0, second step: the data (the app's docs/ACTIVITY-MODEL.md §6).
# The rows go through records.activity_migration.migrate, the same pure function
# the tests run against the shared cases. Every record it touches gets a new
# revision and its fields a clock of now: the phones, realigned after their own
# migration, take the server's version. Not reversible (no reverse code, so
# migrating back raises IrreversibleError): going back would leave the TODO
# status, drop the to-dos and the absences' kind.

import time
import uuid
from datetime import date, datetime, timezone

from django.conf import settings
from django.db import migrations

# field of migrate()'s output -> model field
ENTRY_FIELDS = {
    "kind": "kind", "status": "status", "mode": "mode", "closed_utc_ms": "closed_utc", "deleted_utc_ms": "deleted_utc",
    "company": "company_id", "client": "client_id", "activity": "activity", "notes": "notes", "zone_id": "zone_id",
    "plugin_fields": "plugin_fields", "tags": "tags", "created_utc_ms": "created_utc", "updated_utc_ms": "updated_utc",
    "external_key": "external_key", "due_date": "due_date", "due_time_min": "due_time_min", "plan_date": "plan_date",
    "plan_start_min": "plan_start_min", "duration_min": "duration_min", "priority": "priority",
    "absence_kind": "absence_kind",
}


def _ms(dt):
    return None if dt is None else int(dt.timestamp() * 1000)


def _dt(ms):
    return None if ms is None else datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _iso(d):
    return None if d is None else d.isoformat()


def _str(u):
    return None if u is None else str(u)


def _value(model_field, value):
    if model_field.endswith("_utc"):
        return _dt(value)
    if model_field in ("due_date", "plan_date"):
        return None if value is None else date.fromisoformat(value)
    if model_field in ("company_id", "client_id"):
        return None if value is None else uuid.UUID(value)
    return value


def _clock_name(model_field):
    return model_field[:-3] if model_field.endswith("_id") else model_field


def _hours(w):
    return {"owner": w.owner_id, "day_of_week": w.day_of_week, "start_min": w.start_min, "end_min": w.end_min,
            "deleted": w.deleted}


def rows(apps, org):
    """The org's rows as migrate() reads them."""
    m = {name: apps.get_model("records", name) for name in (
        "Entry", "Segment", "ExternalRef", "Todo", "Absence", "WorkSchedule", "LunchBreak", "DayOff")}
    return {
        "entries": [{
            "uid": str(e.uid), "owner": e.owner_id, "company": _str(e.company_id), "client": _str(e.client_id),
            "notes": e.notes,
            "status": e.status, "mode": e.mode, "todo": _str(e.todo_id),
            "activity": e.activity, "plugin_fields": e.plugin_fields, "created_utc_ms": _ms(e.created_utc),
            "updated_utc_ms": _ms(e.updated_utc), "deleted": e.deleted,
        } for e in m["Entry"].objects.filter(org=org)],
        "segments": [{
            "uid": str(s.uid), "entry": str(s.entry_id), "kind": s.kind, "start_utc_ms": _ms(s.start_utc),
            "end_utc_ms": _ms(s.end_utc), "deleted": s.deleted,
        } for s in m["Segment"].objects.filter(org=org)],
        "external_refs": [{
            "uid": str(r.uid), "entry": str(r.entry_id), "kind": r.kind, "value": r.value, "deleted": r.deleted,
        } for r in m["ExternalRef"].objects.filter(org=org)],
        "todos": [{
            "uid": str(t.uid), "owner": t.owner_id, "kind": t.kind, "title": t.title, "notes": t.notes, "refs": t.refs,
            "company": _str(t.company_id), "client": _str(t.client_id), "external_key": t.external_key,
            "due_date": _iso(t.due_date), "due_time_min": t.due_time_min, "priority": t.priority, "done": t.done,
            "done_utc_ms": _ms(t.done_utc), "created_utc_ms": _ms(t.created_utc), "updated_utc_ms": _ms(t.updated_utc),
            "plan_date": _iso(t.plan_date), "plan_start_min": t.plan_start_min, "duration_min": t.duration_min,
            "ignored_utc_ms": _ms(t.ignored_utc), "plugin_fields": t.plugin_fields,
            "plan_end_date": _iso(t.plan_end_date),
            "deleted": t.deleted,
        } for t in m["Todo"].objects.filter(org=org)],
        "absences": [{
            "uid": str(a.uid), "owner": a.owner_id, "date": a.date.isoformat(), "kind": a.kind, "time_min": a.time_min,
            "deleted": a.deleted,
        } for a in m["Absence"].objects.filter(org=org)],
        "work_schedules": [_hours(w) for w in m["WorkSchedule"].objects.filter(org=org)],
        "lunch_breaks": [_hours(w) for w in m["LunchBreak"].objects.filter(org=org)],
        "days_off": [{"owner": d.owner_id, "date": d.date.isoformat(), "deleted": d.deleted}
                     for d in m["DayOff"].objects.filter(org=org)],
    }


def forward(apps, schema_editor):
    from records.activity_migration import migrate

    Organization = apps.get_model("accounts", "Organization")
    Counter = apps.get_model("sync", "Counter")
    Entry = apps.get_model("records", "Entry")
    Segment = apps.get_model("records", "Segment")
    ExternalRef = apps.get_model("records", "ExternalRef")
    TodoAssignment = apps.get_model("records", "TodoAssignment")
    ActivityAssignment = apps.get_model("records", "ActivityAssignment")
    now = int(time.time() * 1000)  # the clock of every field written
    for org in Organization.objects.all():
        data = rows(apps, org)
        if not data["entries"] and not data["todos"] and not data["absences"]:
            continue
        out = migrate(data, settings.TIME_ZONE)
        counter, _ = Counter.objects.get_or_create(org=org)
        rev = counter.value

        def next_rev():
            nonlocal rev
            rev += 1
            return rev

        existing = {str(e.uid): e for e in Entry.objects.filter(org=org)}
        owners = {}
        for uid, f in sorted(out["entries"].items()):
            if f.get("new"):
                e = Entry(org=org, uid=uuid.UUID(uid), owner_id=f["owner"], clock={})
                for key, name in ENTRY_FIELDS.items():
                    setattr(e, name, _value(name, f.get(key)))
                    e.clock[_clock_name(name)] = now
            else:
                e = existing[uid]
                for key, value in f.items():
                    name = ENTRY_FIELDS[key]
                    value = _value(name, value)
                    if getattr(e, name) != value:
                        setattr(e, name, value)
                        e.clock[_clock_name(name)] = now
            e.rev = next_rev()
            e.save()
            owners[uid] = e.owner_id
        for s in out["segments"]:
            Segment.objects.create(
                org=org, uid=uuid.UUID(s["uid"]), owner_id=owners[s["entry"]], entry_id=uuid.UUID(s["entry"]),
                kind=s["kind"], start_utc=_dt(s["start_utc_ms"]), end_utc=_dt(s["end_utc_ms"]), zone_id=s["zone_id"],
                source=s["source"], planned=s["planned"], rev=next_rev(),
                clock={k: now for k in ("entry", "kind", "start_utc", "end_utc", "zone_id", "source", "planned")},
            )
        for r in out["external_refs"]:
            ExternalRef.objects.create(
                org=org, uid=uuid.UUID(r["uid"]), owner_id=owners[r["entry"]], entry_id=uuid.UUID(r["entry"]),
                kind=r["kind"], value=r["value"], rev=next_rev(),
                clock={k: now for k in ("entry", "kind", "value")},
            )
        Counter.objects.filter(org=org).update(value=rev)
        # who assigned a to-do: kept while it stays a TODO activity
        for a in TodoAssignment.objects.filter(todo__org=org):
            f = out["entries"].get(str(a.todo_id))
            if f and f.get("new") and f["status"] == "TODO":
                ActivityAssignment.objects.create(entry_id=a.todo_id, by_id=a.by_id)
                # auto_now stamps the creation: the assignment's own time back
                ActivityAssignment.objects.filter(entry_id=a.todo_id).update(at=a.at)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0010_organization_offline_maps"),
        ("records", "0007_activity_model"),
        ("sync", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(forward),
    ]
