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
