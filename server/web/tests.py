# SPDX-License-Identifier: GPL-3.0-or-later
from django.test import TestCase, override_settings
from django.urls import clear_script_prefix, reverse, set_script_prefix

from accounts.models import Organization, User


class PathTests(TestCase):
    """Installed under a path: the proxy strips it, URLs carry it (docs/INSTALL.md)."""

    def setUp(self):
        org = Organization.objects.create(name="Infra")
        User.objects.create_user("alice", password="x-x-x-x-x-x", org=org)

    @override_settings(FORCE_SCRIPT_NAME="/activitytrack", BASE_URL="https://at.example.com/activitytrack")
    def test_links_and_pairing_code_carry_the_path(self):
        set_script_prefix("/activitytrack")
        self.addCleanup(clear_script_prefix)
        self.assertEqual(reverse("phones"), "/activitytrack/telefoni/")
        r = self.client.get("/telefoni/")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith("/activitytrack/login/"), r["Location"])
        self.client.login(username="alice", password="x-x-x-x-x-x")
        page = self.client.post("/telefoni/associa/").content.decode()
        self.assertIn("u=https%3A%2F%2Fat.example.com%2Factivitytrack&amp;", page)


class WorkTests(TestCase):
    """The browser's work goes through the sync engine: phones see it as any other change."""

    def setUp(self):
        from records.models import Company
        from sync import engine

        self.org = Organization.objects.create(name="Infra")
        self.alice = User.objects.create_user("alice", password="x-x-x-x-x-x", org=self.org)
        self.boss = User.objects.create_user("boss", password="x-x-x-x-x-x", org=self.org, profile="manager")
        self.alice.manager = self.boss
        self.alice.save()
        self.client.force_login(self.alice)
        engine.apply(self.boss, {"type": "company", "uid": "11111111-1111-4111-8111-111111111111", "fields": {"name": "Acme"}}, central=True)
        self.acme = Company.objects.get(name="Acme")
        engine.apply(self.boss, {"type": "client", "uid": "22222222-2222-4222-8222-222222222222", "fields": {"name": "Banca", "companyUid": str(self.acme.uid)}}, central=True)

    def entry(self):
        from records.models import Entry

        return Entry.objects.filter(owner=self.alice, deleted=False).latest("created_utc")

    def test_pages_render(self):
        for url in ["/", "/attivita/", "/attivita/nuova/", "/dafare/", "/dafare/nuova/", "/anagrafiche/", "/anagrafiche/clienti/", "/report/", "/telefoni/", "/agenda/"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_an_activity_from_start_to_closed(self):
        from records.models import EntryStatus, Segment

        r = self.client.post("/attivita/nuova/", {"when": "now", "client": "22222222-2222-4222-8222-222222222222"})
        e = self.entry()
        self.assertRedirects(r, f"/attivita/{e.uid}/")
        self.assertEqual((e.status, e.mode, e.company_id), (EntryStatus.ACTIVE, "WORK", self.acme.uid))
        self.assertGreater(e.rev, 0)
        self.client.post(f"/attivita/{e.uid}/comando/", {"action": "pause"})
        self.assertEqual(self.entry().mode, "PAUSE")
        self.assertEqual(Segment.objects.filter(entry=e, end_utc__isnull=True).count(), 1)
        r = self.client.post(f"/attivita/{e.uid}/comando/", {"action": "stop"})
        # no description: details missing, to the entry
        self.assertRedirects(r, f"/attivita/{e.uid}/")
        self.assertEqual(self.entry().status, EntryStatus.TO_COMPLETE)
        self.assertFalse(Segment.objects.filter(entry=e, end_utc__isnull=True).exists())
        self.client.post(f"/attivita/{e.uid}/", {"action": "save", "client": "22222222-2222-4222-8222-222222222222", "activity": "Patch", "tags": "#Rete urgente"})
        e = self.entry()
        self.assertEqual((e.status, e.activity, e.tags), (EntryStatus.CLOSED, "Patch", ["rete", "urgente"]))
        page = self.client.get(f"/attivita/{e.uid}/").content.decode()
        self.assertIn("Banca", page)

    def test_a_past_activity_and_its_times(self):
        from records.models import EntryStatus, Segment

        self.client.post("/attivita/nuova/", {"when": "past", "client": "22222222-2222-4222-8222-222222222222", "activity": "Riunione", "start": "2026-10-27T09:00", "end": "2026-10-27T11:30"})
        e = self.entry()
        self.assertEqual(e.status, EntryStatus.CLOSED)
        seg = Segment.objects.get(entry=e)
        self.assertEqual((seg.end_utc - seg.start_utc).total_seconds(), 9000)
        self.client.post(f"/attivita/{e.uid}/", {"action": "segment", "segment": str(seg.uid), "kind": "WORK", "start": "2026-10-27T09:00", "end": "2026-10-27T10:00"})
        seg.refresh_from_db()
        self.assertEqual((seg.end_utc - seg.start_utc).total_seconds(), 3600)
        self.client.post(f"/attivita/{e.uid}/", {"action": "expense", "description": "Parcheggio", "amount": "4,50"})
        r = self.client.get("/report/?from=2026-10-01&to=2026-10-31").content.decode()
        self.assertIn("1:00", r)
        self.assertIn("4,50 €", r)
        csv = self.client.get("/report/csv/?from=2026-10-01&to=2026-10-31").content.decode()
        self.assertIn(";Acme;Banca;;Riunione;CLOSED;2026-10-27 09:00;2026-10-27 10:00;60;0;0;4.50;", csv)

    def test_todos_and_starting_one(self):
        from records.models import Todo

        self.client.post("/dafare/nuova/", {"title": "Rinnovo certificati", "client": "22222222-2222-4222-8222-222222222222", "plan_date": "2026-10-30", "plan_time": "10:00"})
        t = Todo.objects.get(title="Rinnovo certificati")
        self.assertEqual((t.plan_start_min, t.company_id, t.owner_id), (600, self.acme.uid, self.alice.pk))
        self.assertContains(self.client.get(f"/attivita/nuova/?todo={t.uid}"), "Rinnovo certificati")
        self.client.post("/attivita/nuova/", {"when": "now", "todo": str(t.uid), "client": "22222222-2222-4222-8222-222222222222", "activity": "Rinnovo"})
        self.assertEqual(self.entry().todo_id, t.uid)
        self.client.post(f"/dafare/{t.uid}/", {"state": "done"})
        t.refresh_from_db()
        self.assertTrue(t.done and t.done_utc)

    def test_central_master_data_are_read_only_for_users(self):
        from records.models import Role

        self.assertEqual(self.client.get(f"/anagrafiche/aziende/{self.acme.uid}/").status_code, 403)
        self.client.post("/anagrafiche/ruoli/nuova/", {"name": "Sistemista", "central": "on"})
        self.assertIsNotNone(Role.objects.get(name="Sistemista").owner_id)
        self.client.force_login(self.boss)
        self.assertEqual(self.client.get(f"/anagrafiche/aziende/{self.acme.uid}/").status_code, 200)
        self.client.post("/anagrafiche/ruoli/nuova/", {"name": "Formatore", "central": "on"})
        self.assertIsNone(Role.objects.get(name="Formatore").owner_id)

    def test_manager_reads_the_team_reader_the_closed_entries_of_its_companies(self):
        from accounts.models import Profile

        self.client.post("/attivita/nuova/", {"when": "past", "client": "22222222-2222-4222-8222-222222222222", "activity": "Chiusa", "start": "2026-10-27T09:00", "end": "2026-10-27T10:00"})
        self.client.post("/attivita/nuova/", {"when": "now", "client": "22222222-2222-4222-8222-222222222222", "activity": "Aperta"})
        closed = self.entry().__class__.objects.get(activity="Chiusa")
        self.client.force_login(self.boss)
        page = self.client.get("/attivita/?from=2026-10-01").content.decode()
        self.assertIn("Chiusa", page)
        self.assertIn("Aperta", page)
        self.assertNotIn('name="action" value="save"', self.client.get(f"/attivita/{closed.uid}/").content.decode())
        reader = User.objects.create_user("acme", password="x-x-x-x-x-x", org=self.org, profile=Profile.READER)
        reader.companies.add(self.acme)
        self.client.force_login(reader)
        self.assertRedirects(self.client.get("/"), "/report/")
        page = self.client.get("/report/?from=2026-10-01").content.decode()
        self.assertIn("Chiusa", page)
        self.assertNotIn("Aperta", page)
        self.assertEqual(self.client.get("/dafare/").status_code, 403)


class TimesPolicyTests(TestCase):
    """Times locked by the policy: the owner only adds travel and pauses; an admin or the manager fixes them."""

    def setUp(self):
        from datetime import datetime, timezone

        from policies.models import Policy
        from web import ops

        self.org = Organization.objects.create(name="Infra")
        self.boss = User.objects.create_user("boss", password="x-x-x-x-x-x", org=self.org, profile="manager")
        self.alice = User.objects.create_user("alice", password="x-x-x-x-x-x", org=self.org, manager=self.boss)
        self.bob = User.objects.create_user("bob", password="x-x-x-x-x-x", org=self.org, profile="manager")
        Policy.objects.create(org=self.org, data={"settings": {"editableTimes": {"value": False, "locked": True}}}, revision=1)
        t = lambda h: datetime(2026, 10, 30, h, 0, tzinfo=timezone.utc)
        self.e = ops.record_past(self.alice, start_at=t(8), end_at=t(12), activity="Rete")
        # a closed segment from another device inside the first one: to review
        ops.put(self.alice, "segment", ops.new_uid(), {"entryUid": str(self.e.uid), "kind": "WORK", "startUtcMs": ops.ms(t(9)), "endUtcMs": ops.ms(t(10)), "zoneId": "UTC", "source": "MANUAL"})
        ops.put(self.alice, "entry", self.e.uid, {"needsReview": True})
        self.t = t

    def segs(self):
        from records.models import Segment

        return list(Segment.objects.filter(entry=self.e, deleted=False).order_by("start_utc"))

    def post(self, data):
        return self.client.post(f"/attivita/{self.e.uid}/", data)

    def test_owner_cannot_edit_locked_times(self):
        self.client.force_login(self.alice)
        seg = self.segs()[1]
        self.assertEqual(self.post({"action": "segment", "segment": seg.uid, "remove": "1"}).status_code, 403)
        self.assertEqual(self.post({"action": "add_segment", "kind": "WORK", "start": "2026-10-30T14:00", "end": "2026-10-30T15:00"}).status_code, 403)
        # forgotten travel and pauses can always be added
        self.assertEqual(self.post({"action": "add_segment", "kind": "TRAVEL", "start": "2026-10-30T14:00", "end": "2026-10-30T15:00"}).status_code, 302)
        page = self.client.get(f"/attivita/{self.e.uid}/").content.decode()
        self.assertIn("La policy blocca gli orari", page)
        self.assertNotIn('value="segment"', page)
        # a new activity starts now only
        self.assertNotIn('value="past"', self.client.get("/attivita/nuova/").content.decode())
        r = self.client.post("/attivita/nuova/", {"when": "past", "start": "2026-10-29T08:00", "end": "2026-10-29T09:00", "activity": "x"})
        self.assertContains(r, "la policy non consente")

    def test_manager_fixes_the_overlap(self):
        from records.models import Entry

        self.client.force_login(self.boss)
        seg = self.segs()[1]
        self.assertEqual(self.post({"action": "segment", "segment": seg.uid, "remove": "1"}).status_code, 302)
        self.assertEqual(len(self.segs()), 1)
        e = Entry.objects.get(pk=self.e.pk)
        # the record stays the owner's, the mark goes
        self.assertEqual(e.owner, self.alice)
        self.assertFalse(e.needs_review)

    def test_another_manager_cannot(self):
        self.client.force_login(self.bob)
        self.assertEqual(self.client.post(f"/attivita/{self.e.uid}/", {"action": "segment", "segment": self.segs()[1].uid, "remove": "1"}).status_code, 404)


class TagWebTests(TestCase):
    """Predefined tags on the web (app #133)."""

    def setUp(self):
        from web import ops

        self.org = Organization.objects.create(name="Infra")
        self.boss = User.objects.create_user("boss", password="x-x-x-x-x-x", org=self.org, profile="manager")
        self.alice = User.objects.create_user("alice", password="x-x-x-x-x-x", org=self.org, manager=self.boss)
        self.bob = User.objects.create_user("bob", password="x-x-x-x-x-x", org=self.org)
        self.e = ops.start(self.alice, activity="Rete")
        ops.put(self.alice, "entry", self.e.uid, {"tags": ["urgente", "libero"]})
        self.f = ops.start(self.bob, activity="Altro")
        ops.put(self.bob, "entry", self.f.uid, {"tags": ["urgente"]})

    def tag(self, name):
        from records.models import Tag

        return Tag.objects.get(org=self.org, name=name, deleted=False)

    def test_central_tag_with_colour_and_rename_on_every_entry(self):
        from records.models import Entry

        self.client.force_login(self.boss)
        r = self.client.post("/anagrafiche/tag/nuova/", {"name": "#Urgente", "color": str(0xD64545), "central": "1"})
        self.assertEqual(r.status_code, 302)
        t = self.tag("urgente")
        self.assertIsNone(t.owner_id)
        self.assertEqual(t.color, 0xD64545)
        # a second one of the same name is refused
        self.assertContains(self.client.post("/anagrafiche/tag/nuova/", {"name": "urgente", "central": "1"}), "esiste già")
        self.client.post(f"/anagrafiche/tag/{t.uid}/", {"name": "Priorità Alta", "color": str(0xD64545)})
        self.assertEqual(self.tag("priorità-alta").uid, t.uid)
        self.assertEqual(Entry.objects.get(pk=self.e.pk).tags, ["libero", "priorità-alta"])
        self.assertEqual(Entry.objects.get(pk=self.f.pk).tags, ["priorità-alta"])

    def test_personal_rename_stays_with_its_owner(self):
        from records.models import Entry

        self.client.force_login(self.alice)
        self.client.post("/anagrafiche/tag/nuova/", {"name": "urgente"})
        t = self.tag("urgente")
        self.assertEqual(t.owner, self.alice)
        self.client.post(f"/anagrafiche/tag/{t.uid}/", {"name": "subito"})
        self.assertEqual(Entry.objects.get(pk=self.e.pk).tags, ["libero", "subito"])
        self.assertEqual(Entry.objects.get(pk=self.f.pk).tags, ["urgente"])

    def test_dots_and_chips_take_the_colours(self):
        from web import ops

        ops.put(self.boss, "tag", ops.new_uid(), {"name": "urgente", "color": 0xD64545}, central=True)
        self.client.force_login(self.alice)
        home = self.client.get("/").content.decode()
        self.assertIn('class="dots"', home)
        self.assertIn("--tag: #d64545", home)
        self.assertIn("--tag: var(--tag-none)", home)
        sheet = self.client.get(f"/attivita/{self.e.uid}/").content.decode()
        self.assertIn('<span class="tag" style="background: #d64545', sheet)
        self.assertIn('<span class="tag none">libero</span>', sheet)

    def test_free_tags_locked_off(self):
        from policies.models import Policy
        from records.models import Entry
        from web import ops

        Policy.objects.create(org=self.org, data={"settings": {"tagsFreeText": {"value": False, "locked": True}}}, revision=1)
        ops.put(self.boss, "tag", ops.new_uid(), {"name": "rete"}, central=True)
        self.client.force_login(self.alice)
        url = f"/attivita/{self.e.uid}/"
        r = self.client.post(url, {"action": "save", "activity": "Rete", "tags": "urgente libero nuovo"}, follow=True)
        self.assertContains(r, "nuovo non c")
        self.assertEqual(Entry.objects.get(pk=self.e.pk).tags, ["urgente", "libero"])
        # the old free ones stay, a known one is added
        self.client.post(url, {"action": "save", "activity": "Rete", "tags": "urgente libero #Rete"})
        self.assertEqual(Entry.objects.get(pk=self.e.pk).tags, ["libero", "rete", "urgente"])


class ReportingTests(TestCase):
    """The app's day attribution and rounding (core/domain report/, rounding/)."""

    def test_rounding_as_the_app(self):
        from web.reporting import CEIL, CEIL_TOLERANT, FLOOR, Rule

        tolerant = Rule(CEIL_TOLERANT, 15)
        # Rounding.kt examples: 7 -> 15, 19:59 -> 15, 20 -> 30, 34:59 -> 30
        for seconds, minutes in [(0, 0), (7 * 60, 15), (19 * 60 + 59, 15), (20 * 60, 30), (34 * 60 + 59, 30), (45 * 60, 45)]:
            self.assertEqual(tolerant.apply(seconds), minutes, seconds)
        self.assertEqual(Rule(CEIL, 5).apply(61 * 60), 65)
        self.assertEqual(Rule(FLOOR, 5, 5).apply(3 * 60), 5)
        self.assertEqual(Rule(FLOOR, 5, 5).apply(0), 0)

    def test_night_work_stays_on_its_day_or_splits(self):
        from datetime import date, datetime
        from types import SimpleNamespace as S
        from zoneinfo import ZoneInfo

        from web.reporting import Rules, lines

        tz = ZoneInfo("Europe/Rome")
        seg = lambda a, b: S(kind="WORK", start_utc=a, end_utc=b)  # noqa: E731
        # 22:00-04:30: by 06:00 of the next day, all on the start day
        night = [seg(datetime(2026, 10, 27, 22, 0, tzinfo=tz), datetime(2026, 10, 28, 4, 30, tzinfo=tz))]
        out = lines("e", night, Rules(), None, tz)
        self.assertEqual([(x.day, x.work // 60, x.r_work) for x in out], [(date(2026, 10, 27), 390, 390)])
        # until 07:00: split at midnight
        late = [seg(datetime(2026, 10, 27, 22, 0, tzinfo=tz), datetime(2026, 10, 28, 7, 0, tzinfo=tz))]
        out = lines("e", late, Rules(), None, tz)
        self.assertEqual([(x.day, x.work // 60) for x in out], [(date(2026, 10, 27), 120), (date(2026, 10, 28), 420)])

    def test_the_policy_sets_the_rules(self):
        from policies.models import Policy
        from web.reporting import CEIL, Rules, rules_for

        org = Organization.objects.create(name="Infra")
        alice = User.objects.create_user("alice", password="x-x-x-x-x-x", org=org)
        self.assertEqual(rules_for(alice), Rules())
        Policy.objects.create(org=org, data={"settings": {
            "roundingWorkMin": {"value": 30, "locked": True}, "roundingWorkMode": {"value": "CEIL"},
            "carryOverMin": {"value": 120}, "roundingTravelMin": {"value": 7},
        }})
        r = rules_for(alice)
        self.assertEqual((r.work.mode, r.work.quantum, r.carry_over_min), (CEIL, 30, 120))
        # a value the app does not allow: the default
        self.assertEqual(r.travel, Rules().travel)

    def test_lanes_side_by_side(self):
        from web.agenda import lanes

        out = {b["id"]: (b["lane"], b["lanes"]) for b in lanes([
            {"id": "a", "top": 60, "height": 120}, {"id": "b", "top": 90, "height": 30},
            {"id": "c", "top": 130, "height": 30}, {"id": "d", "top": 300, "height": 30},
        ])}
        self.assertEqual(out, {"a": (0, 2), "b": (1, 2), "c": (1, 2), "d": (0, 1)})


class AgendaTests(WorkTests):
    """Agenda by day, week and month (manual §7)."""

    def test_an_entry_and_a_planned_todo_in_every_view(self):
        self.client.post("/attivita/nuova/", {"when": "past", "client": "22222222-2222-4222-8222-222222222222", "activity": "Riunione", "start": "2026-10-27T09:00", "end": "2026-10-27T10:07"})
        self.client.post("/dafare/nuova/", {"title": "Rinnovo certificati", "plan_date": "2026-10-28", "plan_time": "15:00", "duration": "60"})
        day = self.client.get("/agenda/?view=day&date=2026-10-27").content.decode()
        # 1:07 of work rounds to 1:15 (CEIL_TOLERANT 15)
        self.assertIn("09:00–10:07 · 1:15", day)
        self.assertIn("Riunione", day)
        week = self.client.get("/agenda/?view=week&date=2026-10-27").content.decode()
        self.assertIn("☐ Rinnovo certificati", week)
        self.assertIn("15:00–16:00", week)
        self.assertIn('data-plan="/dafare/nuova/"', week)
        month = self.client.get("/agenda/?view=month&date=2026-10-27").content.decode()
        self.assertIn("1:15", month)
        self.assertIn('class="dot todo"', month)
        # the last view comes back
        self.assertIn('class="month"', self.client.get("/agenda/").content.decode())

    def test_a_tag_filter_hides_the_todos(self):
        self.client.post("/attivita/nuova/", {"when": "past", "client": "22222222-2222-4222-8222-222222222222", "activity": "Riunione", "start": "2026-10-27T09:00", "end": "2026-10-27T10:00"})
        self.client.post(f"/attivita/{self.entry().uid}/", {"action": "save", "client": "22222222-2222-4222-8222-222222222222", "activity": "Riunione", "tags": "remoto"})
        self.client.post("/dafare/nuova/", {"title": "Rinnovo", "plan_date": "2026-10-27", "plan_time": "15:00"})
        page = self.client.get("/agenda/?view=day&date=2026-10-27&tag=remoto").content.decode()
        self.assertIn("Riunione", page)
        self.assertNotIn("Rinnovo", page)
        self.assertNotIn("Riunione", self.client.get("/agenda/?view=day&date=2026-10-27&tag=altro").content.decode())

    def test_planning_from_a_free_slot_comes_back(self):
        page = self.client.get("/dafare/nuova/?plan_date=2026-10-28&plan_time=14:30&next=/agenda/%3Fview%3Dday").content.decode()
        self.assertIn('value="2026-10-28"', page)
        self.assertIn('value="14:30"', page)
        r = self.client.post("/dafare/nuova/", {"title": "Chiamata", "plan_date": "2026-10-28", "plan_time": "14:30", "next": "/agenda/?view=day"})
        self.assertRedirects(r, "/agenda/?view=day", fetch_redirect_response=False)
        r = self.client.post("/dafare/nuova/", {"title": "Altrove", "next": "https://evil.example/"})
        self.assertRedirects(r, "/dafare/", fetch_redirect_response=False)

    def test_a_manager_sees_the_team_without_planning(self):
        self.client.post("/attivita/nuova/", {"when": "past", "client": "22222222-2222-4222-8222-222222222222", "activity": "Riunione", "start": "2026-10-27T09:00", "end": "2026-10-27T10:00"})
        self.client.force_login(self.boss)
        page = self.client.get(f"/agenda/?view=day&date=2026-10-27&person={self.alice.pk}").content.decode()
        self.assertIn("Riunione", page)
        self.assertNotIn("data-plan=", page)
        self.assertNotIn("Riunione", self.client.get("/agenda/?view=day&date=2026-10-27").content.decode())

    def test_the_report_rounds_as_the_app(self):
        self.client.post("/attivita/nuova/", {"when": "past", "client": "22222222-2222-4222-8222-222222222222", "activity": "Riunione", "start": "2026-10-27T09:00", "end": "2026-10-27T10:07"})
        page = self.client.get("/report/?from=2026-10-01&to=2026-10-31").content.decode()
        self.assertIn("<b>1:15</b><span class=\"sub\">1:07</span>", page)
        csv = self.client.get("/report/csv/?from=2026-10-01&to=2026-10-31").content.decode()
        self.assertIn(";67;0;0;0.00;;", csv)
        self.assertTrue(csv.strip().endswith(";75;0;0"), csv)


class HomeListTests(WorkTests):
    """The Home list and its filters (app manual §3, HomeTabs.kt homeSections)."""

    def test_sections_and_filters(self):
        from datetime import timedelta

        from django.utils import timezone

        today = timezone.localdate()
        self.client.post("/dafare/nuova/", {"title": "Scaduta", "due_date": str(today - timedelta(days=2))})
        self.client.post("/dafare/nuova/", {"title": "Domani", "plan_date": str(today + timedelta(days=1)), "plan_time": "10:00"})
        self.client.post("/dafare/nuova/", {"title": "Fra un mese", "plan_date": str(today + timedelta(days=40))})
        self.client.post("/dafare/nuova/", {"title": "Senza data"})
        start = timezone.localtime() - timedelta(hours=3)
        self.client.post("/attivita/nuova/", {"when": "past", "client": "22222222-2222-4222-8222-222222222222", "activity": "Chiusa ora",
                                              "start": start.strftime("%Y-%m-%dT%H:%M"), "end": (start + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")})
        self.client.post(f"/attivita/{self.entry().uid}/", {"action": "save", "client": "22222222-2222-4222-8222-222222222222", "activity": "Chiusa ora", "tags": "remoto"})
        self.client.post("/attivita/nuova/", {"when": "past", "client": "22222222-2222-4222-8222-222222222222",
                                              "start": start.strftime("%Y-%m-%dT%H:%M"), "end": (start + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M")})
        # default: today, to-dos and details missing
        page = self.client.get("/").content.decode()
        self.assertIn("In ritardo", page)
        self.assertIn("Scaduta", page)
        self.assertIn("Dettagli mancanti", page)
        self.assertNotIn("Domani", page)
        self.assertIn("Senza data", page)
        self.assertNotIn("Chiusa ora", page)
        # 7 days and the done ones too; remembered afterwards
        page = self.client.get("/?f=1&period=168&show=todo&show=missing&show=done").content.decode()
        self.assertIn("Prossimi giorni", page)
        self.assertIn("Domani", page)
        self.assertNotIn("Fra un mese", page)
        self.assertIn("Chiusa ora", page)
        self.assertIn("Chiusa ora", self.client.get("/").content.decode())
        # a tag keeps the entries with it and hides the to-dos
        page = self.client.get("/?f=1&period=168&show=todo&show=done&tag=remoto").content.decode()
        self.assertIn("Chiusa ora", page)
        self.assertNotIn("Scaduta", page)
        # only done
        page = self.client.get("/?f=1&period=0&show=done").content.decode()
        self.assertNotIn("Scaduta", page)
        self.assertNotIn("Fra un mese", page)

    def test_a_started_todo_leaves_the_list(self):
        from records.models import Todo

        self.client.post("/dafare/nuova/", {"title": "Avviata", "due_date": "2026-01-01"})
        t = Todo.objects.get(title="Avviata")
        self.assertIn("Avviata", self.client.get("/").content.decode())
        self.client.post("/attivita/nuova/", {"when": "now", "todo": str(t.uid), "client": "22222222-2222-4222-8222-222222222222"})
        page = self.client.get("/").content.decode()
        self.assertNotIn('<b>Avviata</b>', page)

    def test_overdue_rules(self):
        from datetime import date, datetime
        from types import SimpleNamespace as S
        from zoneinfo import ZoneInfo

        from web.homelist import state

        tz = ZoneInfo("Europe/Rome")
        t = lambda **k: S(**{"done": False, "ignored_utc": None, "plan_date": None, "plan_start_min": None, "duration_min": None, "due_date": None, "due_time_min": None, **k})  # noqa: E731
        now = datetime(2026, 10, 30, 12, 0, tzinfo=tz)
        self.assertEqual(state(t(due_date=date(2026, 10, 30)), now, tz), "open")
        self.assertEqual(state(t(due_date=date(2026, 10, 30), due_time_min=600), now, tz), "overdue")
        self.assertEqual(state(t(plan_date=date(2026, 10, 30)), now, tz), "planned")
        self.assertEqual(state(t(plan_date=date(2026, 10, 30), plan_start_min=600, duration_min=60), now, tz), "overdue")
        self.assertEqual(state(t(plan_date=date(2026, 10, 30), plan_start_min=660, duration_min=90), now, tz), "planned")


class TodoAssignmentTests(WorkTests):
    """A manager gives to-dos to the team (ARCHITECTURE §5): the phones follow the owner."""

    def test_assign_and_reassign(self):
        from records.models import Todo
        from sync import engine

        carl = User.objects.create_user("carl", password="x-x-x-x-x-x", org=self.org, manager=self.boss)
        outsider = User.objects.create_user("dora", password="x-x-x-x-x-x", org=self.org)
        self.client.force_login(self.boss)
        page = self.client.get("/dafare/nuova/").content.decode()
        self.assertIn('name="owner"', page)
        self.assertNotIn(">dora<", page)
        cursor = engine.head(self.org)
        r = self.client.post("/dafare/nuova/", {"title": "Rinnovo", "owner": str(self.alice.pk)})
        self.assertRedirects(r, "/dafare/", fetch_redirect_response=False)
        t = Todo.objects.get(title="Rinnovo")
        self.assertEqual(t.owner, self.alice)
        self.assertEqual(t.assignment.by, self.boss)
        self.assertEqual([x["uid"] for x in engine.pull(self.alice, cursor)["records"] if x["type"] == "todo"], [str(t.uid)])
        self.assertFalse([x for x in engine.pull(self.boss, cursor)["records"] if x["type"] == "todo"])
        # the team's list and the person's
        self.assertIn("Rinnovo", self.client.get("/dafare/?person=all").content.decode())
        self.assertNotIn("Rinnovo", self.client.get("/dafare/").content.decode())
        # to carl: alice's phones drop it, carl's get it
        cursor = engine.head(self.org)
        self.client.post(f"/dafare/{t.uid}/", {"title": "Rinnovo", "owner": str(carl.pk), "notes": "urgente"})
        t.refresh_from_db()
        self.assertEqual((t.owner, t.notes), (carl, "urgente"))
        self.assertEqual(engine.pull(self.alice, cursor)["gone"], [{"type": "todo", "uid": str(t.uid)}])
        self.assertEqual([x["uid"] for x in engine.pull(carl, cursor)["records"]], [str(t.uid)])
        # the assignee sees who gave it and starts it; the manager does not start it
        self.client.force_login(carl)
        page = self.client.get(f"/dafare/{t.uid}/").content.decode()
        self.assertIn("Assegnata da boss", page)
        self.assertIn("?todo=", page)
        self.assertNotIn('name="owner"', page)
        # someone outside the team may not get or read it
        self.client.force_login(self.boss)
        self.assertEqual(self.client.post(f"/dafare/{t.uid}/", {"title": "x", "owner": str(outsider.pk)}).status_code, 403)
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(f"/dafare/{t.uid}/").status_code, 404)

    def test_a_manager_marks_a_team_todo_done(self):
        from records.models import Todo

        self.client.force_login(self.boss)
        self.client.post("/dafare/nuova/", {"title": "Chiamata", "owner": str(self.alice.pk)})
        t = Todo.objects.get(title="Chiamata")
        self.client.post(f"/dafare/{t.uid}/", {"state": "done"})
        t.refresh_from_db()
        self.assertEqual((t.done, t.owner), (True, self.alice))


class ManagerCompaniesTests(WorkTests):
    """A manager reads every entry done for the companies they are responsible for, an admin's too."""

    def test_an_admins_entry_in_the_managers_company(self):
        from accounts.models import Profile

        admin = User.objects.create_user("root", password="x-x-x-x-x-x", org=self.org, profile=Profile.ADMIN)
        self.client.force_login(admin)
        self.client.post("/attivita/nuova/", {"when": "past", "client": "22222222-2222-4222-8222-222222222222", "activity": "Lavoro admin", "start": "2026-10-27T09:00", "end": "2026-10-27T10:00"})
        from records.models import Entry

        entry = Entry.objects.get(activity="Lavoro admin")
        self.client.force_login(self.boss)
        self.assertNotIn("Lavoro admin", self.client.get("/attivita/?from=2026-10-01").content.decode())
        self.boss.companies.add(self.acme)
        self.assertIn("Lavoro admin", self.client.get("/attivita/?from=2026-10-01").content.decode())
        self.assertIn("Lavoro admin", self.client.get("/report/?from=2026-10-01").content.decode())
        self.assertIn(f'value="{admin.pk}"', self.client.get("/report/?from=2026-10-01").content.decode())
        self.assertIn("Lavoro admin", self.client.get(f"/agenda/?view=day&date=2026-10-27&person={admin.pk}").content.decode())
        # read only: the admin is not in the manager's team
        page = self.client.get(f"/attivita/{entry.uid}/").content.decode()
        self.assertNotIn('name="action" value="segment"', page)


class UsersTests(WorkTests):
    """Users managed on the web by an admin; everyone changes their own password."""

    def setUp(self):
        from accounts.models import Profile

        super().setUp()
        self.root = User.objects.create_user("root", password="x-x-x-x-x-x", org=self.org, profile=Profile.ADMIN)

    def test_only_admins(self):
        self.assertEqual(self.client.get("/utenti/").status_code, 403)
        self.client.force_login(self.boss)
        self.assertEqual(self.client.get(f"/utenti/{self.alice.pk}/").status_code, 403)

    def test_create_edit_and_password(self):
        self.client.force_login(self.root)
        self.assertIn("alice", self.client.get("/utenti/").content.decode())
        base = {"username": "erika", "first_name": "Erika", "profile": "user", "manager": str(self.boss.pk), "active": "on"}
        r = self.client.post("/utenti/nuovo/", {**base, "password": "corta", "password2": "corta"})
        self.assertContains(r, "Correggi i campi")
        self.assertFalse(User.objects.filter(username="erika").exists())
        r = self.client.post("/utenti/nuovo/", {**base, "password": "una-password-lunga", "password2": "una-password-lunga"})
        erika = User.objects.get(username="erika")
        self.assertRedirects(r, f"/utenti/{erika.pk}/", fetch_redirect_response=False)
        self.assertEqual((erika.org, erika.manager, erika.is_active), (self.org, self.boss, True))
        self.assertTrue(erika.check_password("una-password-lunga"))
        # a manager responsible for a company; a duplicate name refused
        self.client.post(f"/utenti/{self.boss.pk}/", {"username": "boss", "profile": "manager", "active": "on", "companies": [str(self.acme.uid)]})
        self.assertEqual(list(self.boss.companies.all()), [self.acme])
        r = self.client.post(f"/utenti/{erika.pk}/", {**base, "username": "ALICE"})
        self.assertContains(r, "esiste già")
        # deactivated: no web login, no sync
        self.client.post(f"/utenti/{erika.pk}/", {**{k: v for k, v in base.items() if k != "active"}})
        erika.refresh_from_db()
        self.assertFalse(erika.is_active)
        self.client.post(f"/utenti/{erika.pk}/password/", {"password": "altra-password-lunga", "password2": "altra-password-lunga"})
        erika.refresh_from_db()
        self.assertTrue(erika.check_password("altra-password-lunga"))

    def test_an_admin_does_not_lock_themselves_out(self):
        self.client.force_login(self.root)
        r = self.client.post(f"/utenti/{self.root.pk}/", {"username": "root", "profile": "user"})
        self.assertContains(r, "Non puoi toglierti il profilo")
        self.assertContains(r, "Non puoi disattivare")
        self.root.refresh_from_db()
        self.assertTrue(self.root.is_admin and self.root.is_active)

    def test_own_password(self):
        r = self.client.post("/password/", {"old": "sbagliata", "password": "nuova-password-lunga", "password2": "nuova-password-lunga"})
        self.assertContains(r, "non è corretta")
        r = self.client.post("/password/", {"old": "x-x-x-x-x-x", "password": "nuova-password-lunga", "password2": "nuova-password-lunga"})
        self.assertRedirects(r, "/", fetch_redirect_response=False)
        self.alice.refresh_from_db()
        self.assertTrue(self.alice.check_password("nuova-password-lunga"))
        # still logged in
        self.assertEqual(self.client.get("/dafare/").status_code, 200)


class SecurityTests(TestCase):
    """Phase 9: what the security review fixed."""

    def setUp(self):
        self.org = Organization.objects.create(name="Infra")
        self.alice = User.objects.create_user("alice", password="x-x-x-x-x-x", org=self.org)

    def test_password_guessing_is_held_back(self):
        for _ in range(10):
            self.assertContains(self.client.post("/login/", {"username": "alice", "password": "wrong"}), "errati")
        r = self.client.post("/login/", {"username": "alice", "password": "x-x-x-x-x-x"})
        self.assertContains(r, "Troppi tentativi")
        self.assertNotIn("_auth_user_id", self.client.session)
        # another address is not held back
        r = self.client.post("/login/", {"username": "alice", "password": "x-x-x-x-x-x"}, REMOTE_ADDR="", HTTP_X_FORWARDED_FOR="203.0.113.9")
        self.assertEqual(r.status_code, 302)

    def test_an_html_attachment_is_downloaded_not_shown(self):
        from types import SimpleNamespace as S

        from django.http import HttpResponse

        from sync.views import _guarded

        page = _guarded(HttpResponse(content_type="text/html"), S(mime="text/html", uid="u1"))
        self.assertTrue(page["Content-Disposition"].startswith("attachment"))
        self.assertIn("sandbox", page["Content-Security-Policy"])
        photo = _guarded(HttpResponse(content_type="image/jpeg"), S(mime="image/jpeg", uid="u2"))
        self.assertTrue(photo["Content-Disposition"].startswith("inline"))
        svg = _guarded(HttpResponse(content_type="image/svg+xml"), S(mime="image/svg+xml", uid="u3"))
        self.assertTrue(svg["Content-Disposition"].startswith("attachment"))


class MfaTests(TestCase):
    """Two-step login (web/mfa.py): authenticator app or email code, recovery codes, the organisation's rule."""

    def setUp(self):
        from accounts.models import Profile

        self.org = Organization.objects.create(name="Infra")
        self.alice = User.objects.create_user("alice", password="x-x-x-x-x-x", org=self.org, email="alice@example.com")
        self.root = User.objects.create_user("root", password="x-x-x-x-x-x", org=self.org, profile=Profile.ADMIN)

    def test_rfc_6238_vectors(self):
        from web.mfa import totp

        secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # "12345678901234567890"
        self.assertEqual(totp(secret, 59 // 30), "287082")
        self.assertEqual(totp(secret, 1111111109 // 30), "081804")
        self.assertEqual(totp(secret, 2000000000 // 30), "279037")

    def login(self, user="alice"):
        return self.client.post("/login/", {"username": user, "password": "x-x-x-x-x-x"})

    def test_authenticator_app(self):
        import time

        from web.mfa import totp

        self.client.force_login(self.alice)
        self.client.post("/sicurezza/", {"action": "totp_start"})
        secret = self.client.session["mfa_setup"]["secret"]
        self.assertContains(self.client.get("/sicurezza/"), "<svg")
        now = int(time.time() // 30)
        r = self.client.post("/sicurezza/", {"action": "confirm", "code": totp(secret, now)})
        self.assertContains(r, "Codici di recupero")
        codes = r.content.decode().split("<code>")[1:]
        recovery = codes[0].split("</code>")[0]
        self.alice.refresh_from_db()
        self.assertEqual((self.alice.mfa_method, len(self.alice.mfa_recovery)), ("totp", 10))
        self.client.logout()
        # the password alone does not log in
        self.assertRedirects(self.login(), "/login/codice/", fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertContains(self.client.post("/login/codice/", {"code": "000000"}), "non valido")
        # the code used to set up is spent: the next step's code works
        self.assertContains(self.client.post("/login/codice/", {"code": totp(secret, now)}), "non valido")
        r = self.client.post("/login/codice/", {"code": totp(secret, now + 1)})
        self.assertRedirects(r, "/", fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.alice.pk)
        # a recovery code, once
        self.client.logout()
        self.login()
        self.assertRedirects(self.client.post("/login/codice/", {"code": recovery}), "/", fetch_redirect_response=False)
        self.client.logout()
        self.login()
        self.assertContains(self.client.post("/login/codice/", {"code": recovery}), "non valido")

    def test_too_many_wrong_codes(self):
        from web.mfa import new_secret

        self.alice.mfa_method, self.alice.totp_secret = "totp", new_secret()
        self.alice.save()
        self.login()
        for _ in range(4):
            self.client.post("/login/codice/", {"code": "000000"})
        self.assertRedirects(self.client.post("/login/codice/", {"code": "000000"}), "/login/", fetch_redirect_response=False)
        self.assertRedirects(self.client.get("/login/codice/"), "/login/", fetch_redirect_response=False)

    def test_email_code(self):
        import re

        from django.core import mail

        with self.settings(EMAIL_HOST="smtp.example.com"):
            self.client.force_login(self.alice)
            self.client.post("/sicurezza/", {"action": "email_start"})
            code = re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)
            self.assertEqual(mail.outbox[-1].to, ["alice@example.com"])
            self.assertContains(self.client.post("/sicurezza/", {"action": "confirm", "code": code}), "Codici di recupero")
            self.client.logout()
            self.assertRedirects(self.login(), "/login/codice/", fetch_redirect_response=False)
            code = re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)
            self.assertRedirects(self.client.post("/login/codice/", {"code": code}), "/", fetch_redirect_response=False)
        # without mail on the server, no email choice
        self.assertNotContains(self.client.get("/sicurezza/"), 'value="email_start"')

    def test_the_organisation_requires_it(self):
        self.client.force_login(self.root)
        self.client.post("/utenti/", {"mfa_required": "admins"})
        self.org.refresh_from_db()
        self.assertEqual(self.org.mfa_required, "admins")
        # the admin without a second step only sets it up
        self.assertRedirects(self.client.get("/utenti/"), "/sicurezza/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/sicurezza/").status_code, 200)
        # a user is not concerned
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get("/dafare/").status_code, 200)
        # phones keep syncing: the API is not stopped
        self.client.force_login(self.root)
        self.assertNotEqual(self.client.get("/api/v1/sync/head").status_code, 302)

    def test_admin_resets_a_lost_phone(self):
        from web.mfa import new_secret

        self.alice.mfa_method, self.alice.totp_secret = "totp", new_secret()
        self.alice.save()
        self.client.force_login(self.root)
        self.assertContains(self.client.get(f"/utenti/{self.alice.pk}/"), "Azzera")
        self.client.post(f"/utenti/{self.alice.pk}/verifica/")
        self.alice.refresh_from_db()
        self.assertEqual((self.alice.mfa_method, self.alice.totp_secret), ("", ""))
        self.client.logout()
        self.assertRedirects(self.login(), "/", fetch_redirect_response=False)
