# SPDX-License-Identifier: GPL-3.0-or-later
"""The data migration 0007-0009 on a real database: to-dos and absences of before become activities."""

import uuid
from datetime import date, datetime, timezone

from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from records.activity_migration import uid_for

BEFORE = [("records", "0006_todo_absence")]


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


class ActivityMigrationTests(TransactionTestCase):

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        # the other apps as they are now, records as [targets] say
        others = [n for n in executor.loader.graph.leaf_nodes() if n[0] not in {app for app, _ in targets}]
        return executor.loader.project_state(others + targets).apps

    def test_todos_and_absences_become_activities(self):
        old = self.back_to_0006()
        Organization, User = old.get_model("accounts", "Organization"), old.get_model("accounts", "User")
        Company, Entry, Segment = (old.get_model("records", n) for n in ("Company", "Entry", "Segment"))
        Todo, TodoAssignment, Absence = (old.get_model("records", n) for n in ("Todo", "TodoAssignment", "Absence"))
        WorkSchedule, LunchBreak, ExternalRef = (old.get_model("records", n) for n in ("WorkSchedule", "LunchBreak", "ExternalRef"))
        from sync.models import Counter  # unchanged by these migrations
        org = Organization.objects.create(name="Infra")
        Counter.objects.create(org_id=org.pk, value=100)
        alice = User.objects.create(username="alice", org=org)
        boss = User.objects.create(username="boss", org=org, profile="manager")
        acme = Company.objects.create(org=org, name="Acme", rev=1)
        u = lambda: uuid.uuid4()  # noqa: E731
        t0 = at("2026-11-02T08:00:00")

        def todo(**k):
            return Todo.objects.create(org=org, owner=alice, created_utc=t0, updated_utc=t0, rev=2, **k)

        def entry(status, **k):
            return Entry.objects.create(org=org, owner=alice, status=status, zone_id="Europe/Rome", created_utc=t0, updated_utc=t0, rev=3, **k)

        plain = todo(title="Rinnovo", company=acme, priority=1, refs="INC42\nhttps://x.org/1", external_key="K1",
                     plan_date=date(2026, 11, 20), plan_start_min=600, duration_min=60)
        TodoAssignment.objects.create(todo=plain, by=boss)
        worked = todo(title="Patch", done=True, done_utc=t0, priority=5)
        first = entry("TO_COMPLETE", todo=worked)
        Segment.objects.create(org=org, owner=alice, entry=first, kind="WORK", start_utc=at("2026-11-03T08:00:00"),
                               end_utc=at("2026-11-03T09:00:00"), zone_id="Europe/Rome", rev=4)
        second = entry("ACTIVE", todo=worked, mode="WORK", activity="Seconda")
        Segment.objects.create(org=org, owner=alice, entry=second, kind="WORK", start_utc=at("2026-11-04T08:00:00"), zone_id="Europe/Rome", rev=4)
        WorkSchedule.objects.create(org=org, owner=alice, day_of_week=3, start_min=540, end_min=1020, rev=5)
        LunchBreak.objects.create(org=org, owner=alice, day_of_week=3, start_min=780, end_min=840, rev=5)
        absent = todo(kind="ABSENCE", title="Malattia", plan_date=date(2026, 11, 3), plan_end_date=date(2026, 11, 5))
        legacy = Absence.objects.create(org=org, owner=alice, date=date(2026, 11, 4), kind="FULL_DAY", rev=6)

        self.migrate(self.leaves())
        from records.models import ActivityAssignment, Entry, ExternalRef, Segment

        a = Entry.objects.get(uid=plain.uid)
        self.assertEqual((a.kind, a.status, a.activity, a.company_id, a.priority, a.external_key, a.plan_start_min, a.owner_id),
                         ("ACTIVITY", "TODO", "Rinnovo", acme.uid, 5, "K1", 600, alice.pk))
        self.assertEqual(sorted(ExternalRef.objects.filter(entry=a).values_list("kind", "value")), [("TICKET", "INC42"), ("URL", "https://x.org/1")])
        self.assertEqual(ActivityAssignment.objects.get(entry=a).by_id, boss.pk)
        self.assertGreater(a.rev, 100)
        self.assertIn("priority", a.clock)
        # the earliest entry absorbs the done to-do; the other one points to it
        f, s = Entry.objects.get(uid=first.uid), Entry.objects.get(uid=second.uid)
        self.assertEqual((f.status, f.activity, f.priority, f.closed_utc), ("CLOSED", "Patch", 1, at("2026-11-03T09:00:00")))
        self.assertEqual((s.status, s.mode, s.activity), ("RUNNING", "WORK", "Seconda"))
        ref = ExternalRef.objects.get(entry=s)
        self.assertEqual((ref.uid, ref.kind, ref.value, ref.owner_id), (uuid.UUID(uid_for(worked.uid, f"link/{second.uid}")), "UUID", str(first.uid), alice.pk))
        self.assertFalse(Entry.objects.filter(uid=worked.uid).exists())
        # the absence: Wednesday only (the schedule's day), cut by the lunch break
        ab = Entry.objects.get(uid=absent.uid)
        self.assertEqual((ab.kind, ab.status, ab.absence_kind), ("ABSENCE", "CLOSED", "SICK"))
        segs = list(Segment.objects.filter(entry=ab).order_by("start_utc"))
        self.assertEqual([(x.uid, x.kind, x.planned, x.owner_id) for x in segs],
                         [(uuid.UUID(uid_for(absent.uid, f"abs/2026-11-04/{n}")), "ABSENCE", False, alice.pk) for n in (0, 1)])
        self.assertEqual((segs[0].start_utc, segs[1].end_utc, ab.closed_utc), (at("2026-11-04T08:00:00"), at("2026-11-04T16:00:00"), segs[1].end_utc))
        self.assertEqual(Entry.objects.get(uid=legacy.uid).kind, "ABSENCE")
        top = max(max(m.objects.values_list("rev", flat=True)) for m in (Entry, Segment, ExternalRef))
        self.assertEqual(Counter.objects.get(org_id=org.pk).value, top)

    def back_to_0006(self):
        # 0008 cannot be undone: it is taken off the record without running (the tables are empty here)
        self.migrate([("records", "0008_activity_data")])
        MigrationExecutor(connection).migrate([("records", "0007_activity_model")], fake=True)
        return self.migrate(BEFORE)

    def test_going_back_is_refused(self):
        """Back to 0.9 would leave TODO entries and kindless absences: the way back is the backup."""
        from django.db.migrations.exceptions import IrreversibleError

        self.migrate(self.leaves())
        try:
            with self.assertRaises(IrreversibleError):
                self.migrate(BEFORE)
        finally:
            self.migrate(self.leaves())

    def test_refused_going_back_loses_nothing(self):
        """The refusal comes at 0008, after 0010 and 0009 are undone: migrating forward again restores 1.0 as it was."""
        from django.db.migrations.exceptions import IrreversibleError

        from accounts.models import Organization, User
        from records.models import ActivityAssignment, Entry, Segment

        self.migrate(self.leaves())
        org = Organization.objects.create(name="Infra")
        alice = User.objects.create(username="alice", org=org)
        boss = User.objects.create(username="boss", org=org, profile="manager")
        t0 = at("2026-11-02T08:00:00")
        e = Entry.objects.create(org=org, owner=alice, kind="ACTIVITY", status="TODO", activity="Rinnovo", external_key="K1",
                                 zone_id="Europe/Rome", created_utc=t0, updated_utc=t0, rev=1)
        Segment.objects.create(org=org, owner=alice, entry=e, kind="WORK", start_utc=t0, end_utc=at("2026-11-02T09:00:00"),
                               zone_id="Europe/Rome", rev=2)
        ActivityAssignment.objects.create(entry=e, by=boss)

        def snapshot():
            return (list(Entry.objects.order_by("uid").values()), list(Segment.objects.order_by("uid").values()),
                    list(ActivityAssignment.objects.values_list("entry_id", "by_id")))

        before = snapshot()
        try:
            with self.assertRaises(IrreversibleError):
                self.migrate(BEFORE)
            applied = {name for app, name in MigrationExecutor(connection).loader.applied_migrations if app == "records"}
            self.assertIn("0008_activity_data", applied)
            self.assertNotIn("0009_drop_todo", applied)
            self.assertNotIn("0010_drop_stale_scope_exits", applied)
            self.assertEqual(snapshot(), before)  # the half-way state keeps every row
        finally:
            self.migrate(self.leaves())
        self.assertEqual(snapshot(), before)  # and so does the way forward

    def leaves(self):
        executor = MigrationExecutor(connection)
        return executor.loader.graph.leaf_nodes()


class StaleScopeExitTests(TransactionTestCase):
    """0010: the exits of the removed types todo and absence would be pulled as gone entries of an unknown type."""

    def test_exits_of_removed_types_go(self):
        from importlib import import_module

        from django.apps import apps

        from accounts.models import Organization, User
        from sync import engine
        from sync.models import ScopeExit

        org = Organization.objects.create(name="Infra")
        alice = User.objects.create_user("alice", password="x", org=org)
        for rev, type_ in enumerate(("todo", "absence", "entry", "segment"), start=1):
            ScopeExit.objects.create(org=org, user=alice, type=type_, uid=uuid.uuid4(), rev=rev)
        with transaction.atomic():  # select_for_update (next_rev) needs a transaction, as in the sync views
            for _ in range(4):
                engine.next_rev(org)
        self.assertEqual({g["type"] for g in engine.pull(alice, 0)["gone"]}, {"todo", "absence", "entry", "segment"})
        import_module("records.migrations.0010_drop_stale_scope_exits").forward(apps, None)
        self.assertEqual({g["type"] for g in engine.pull(alice, 0)["gone"]}, {"entry", "segment"})
