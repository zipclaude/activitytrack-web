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
        self.assertEqual(reverse("phones"), "/activitytrack/dispositivi/")
        r = self.client.get("/dispositivi/")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith("/activitytrack/login/"), r["Location"])
        self.client.login(username="alice", password="x-x-x-x-x-x")
        page = self.client.post("/dispositivi/associa/").content.decode()
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
        for url in ["/", "/attivita/", "/attivita/nuova/", "/cestino/", "/dafare/", "/dafare/nuova/", "/anagrafiche/", "/anagrafiche/clienti/", "/report/", "/dispositivi/", "/agenda/"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_an_activity_from_start_to_closed(self):
        from records.models import EntryStatus, Segment

        r = self.client.post("/attivita/nuova/", {"when": "now", "client": "22222222-2222-4222-8222-222222222222"})
        e = self.entry()
        self.assertRedirects(r, f"/attivita/{e.uid}/")
        self.assertEqual((e.status, e.mode, e.company_id, e.kind), (EntryStatus.RUNNING, "WORK", self.acme.uid, "ACTIVITY"))
        self.assertGreater(e.rev, 0)
        self.client.post(f"/attivita/{e.uid}/comando/", {"action": "pause"})
        self.assertEqual(self.entry().mode, "PAUSE")
        self.assertEqual(Segment.objects.filter(entry=e, end_utc__isnull=True).count(), 1)
        # no "Sospendi" button: an activity is suspended when another starts (ops.suspend)
        self.assertNotContains(self.client.get("/"), "Sospendi")
        self.client.post(f"/attivita/{e.uid}/comando/", {"action": "suspend"})
        self.assertEqual(self.entry().mode, "PAUSE")
        from web import ops

        ops.suspend(self.alice, self.entry())
        self.assertEqual((self.entry().status, self.entry().mode), (EntryStatus.RUNNING, None))
        self.assertContains(self.client.get("/"), "Sospesa")
        self.client.post(f"/attivita/{e.uid}/comando/", {"action": "work"})
        r = self.client.post(f"/attivita/{e.uid}/comando/", {"action": "stop"})
        # a client and some work: complete (ACTIVITY-MODEL §2.4), back home
        self.assertRedirects(r, "/")
        e = self.entry()
        self.assertEqual((e.status, e.mode), (EntryStatus.CLOSED, None))
        self.assertIsNotNone(e.closed_utc)
        self.assertFalse(Segment.objects.filter(entry=e, end_utc__isnull=True).exists())
        self.client.post(f"/attivita/{e.uid}/", {"action": "save", "client": "22222222-2222-4222-8222-222222222222", "activity": "Patch", "tags": "#Rete urgente"})
        e = self.entry()
        self.assertEqual((e.status, e.activity, e.tags), (EntryStatus.CLOSED, "Patch", ["rete", "urgente"]))
        page = self.client.get(f"/attivita/{e.uid}/").content.decode()
        self.assertIn("Banca", page)
        # reopened: running again with work open, then into the bin
        self.client.post(f"/attivita/{e.uid}/", {"action": "reopen"})
        e = self.entry()
        self.assertEqual((e.status, e.mode, e.closed_utc), (EntryStatus.RUNNING, "WORK", None))
        self.client.post(f"/attivita/{e.uid}/", {"action": "delete"})
        e = self.entry()
        self.assertIsNotNone(e.deleted_utc)
        self.assertEqual(self.client.get(f"/attivita/{e.uid}/").status_code, 404)
        self.assertContains(self.client.get("/cestino/"), "Patch")
        self.client.post("/cestino/", {"action": "restore", "uid": str(e.uid)})
        self.assertIsNone(self.entry().deleted_utc)

    def test_without_a_client_details_are_missing(self):
        r = self.client.post("/attivita/nuova/", {"when": "now"})
        e = self.entry()
        r = self.client.post(f"/attivita/{e.uid}/comando/", {"action": "stop"})
        self.assertRedirects(r, f"/attivita/{e.uid}/")
        self.assertEqual(self.entry().status, "CLOSED")

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
        from records.models import Entry, Segment

        self.client.post("/dafare/nuova/", {"title": "Rinnovo certificati", "client": "22222222-2222-4222-8222-222222222222", "plan_date": "2026-10-30",
                                            "plan_time": "10:00", "priority": "4"})
        t = Entry.objects.get(activity="Rinnovo certificati")
        self.assertEqual((t.kind, t.status, t.plan_start_min, t.company_id, t.owner_id, t.priority), ("ACTIVITY", "TODO", 600, self.acme.uid, self.alice.pk, 4))
        self.assertContains(self.client.get("/dafare/"), "Rinnovo certificati")
        self.assertContains(self.client.get(f"/attivita/nuova/?todo={t.uid}"), "Rinnovo certificati")
        # a planned segment goes when it starts (§1.2)
        from web import ops

        ops.put(self.alice, "segment", ops.new_uid(), {"entryUid": str(t.uid), "kind": "WORK", "startUtcMs": 1761811200000,
                                                       "endUtcMs": 1761814800000, "zoneId": "Europe/Rome", "planned": True})
        self.client.post("/attivita/nuova/", {"when": "now", "todo": str(t.uid), "client": "22222222-2222-4222-8222-222222222222", "activity": "Rinnovo"})
        # the same activity, started
        t.refresh_from_db()
        self.assertEqual((t.status, t.mode, t.activity, t.plan_start_min), ("RUNNING", "WORK", "Rinnovo", 600))
        self.assertEqual(Entry.objects.filter(owner=self.alice, deleted=False).count(), 1)
        self.assertEqual([(s.kind, s.planned, s.end_utc) for s in Segment.objects.filter(entry=t, deleted=False)], [("WORK", False, None)])
        # another one ticked off: closed without segments, "done" on the to-do pages
        self.client.post("/dafare/nuova/", {"title": "Chiamare"})
        c = Entry.objects.get(activity="Chiamare")
        self.client.post(f"/dafare/{c.uid}/", {"state": "done"})
        c.refresh_from_db()
        self.assertEqual(c.status, "CLOSED")
        self.assertTrue(c.closed_utc)
        self.assertContains(self.client.get("/dafare/?show=done"), "Chiamare")
        self.client.post(f"/dafare/{c.uid}/", {"state": "open"})
        c.refresh_from_db()
        self.assertEqual((c.status, c.closed_utc), ("TODO", None))
        self.client.post(f"/dafare/{c.uid}/", {"action": "delete"})
        c.refresh_from_db()
        self.assertIsNotNone(c.deleted_utc)
        # in the bin only, restorable there; deleted for good only from the bin
        self.assertNotContains(self.client.get("/dafare/"), "Chiamare")
        self.assertContains(self.client.get("/cestino/"), "Chiamare")
        self.client.post("/cestino/", {"action": "restore", "uid": str(c.uid)})
        c.refresh_from_db()
        self.assertIsNone(c.deleted_utc)
        self.assertContains(self.client.get("/dafare/"), "Chiamare")
        self.client.post("/cestino/", {"action": "purge", "uid": str(c.uid)})
        self.assertTrue(Entry.objects.filter(uid=c.uid, deleted=False).exists())
        self.client.post(f"/dafare/{c.uid}/", {"action": "delete"})
        self.client.post("/cestino/", {"action": "purge", "uid": str(c.uid)})
        self.assertTrue(Entry.objects.get(uid=c.uid).deleted)
        self.assertNotContains(self.client.get("/cestino/"), "Chiamare")

    def test_central_master_data_are_read_only_for_users(self):
        from records.models import Role

        self.assertEqual(self.client.get(f"/anagrafiche/aziende/{self.acme.uid}/").status_code, 403)
        self.client.post("/anagrafiche/ruoli/nuova/", {"name": "Sistemista", "central": "on"})
        self.assertIsNotNone(Role.objects.get(name="Sistemista").owner_id)
        self.client.force_login(self.boss)
        self.assertEqual(self.client.get(f"/anagrafiche/aziende/{self.acme.uid}/").status_code, 200)
        self.client.post("/anagrafiche/ruoli/nuova/", {"name": "Formatore", "central": "on"})
        self.assertIsNone(Role.objects.get(name="Formatore").owner_id)

    def test_companies_and_clients_take_a_colour(self):
        from records.models import Client, Company

        self.client.force_login(self.boss)
        self.assertContains(self.client.get(f"/anagrafiche/aziende/{self.acme.uid}/"), 'name="color"')
        self.client.post(f"/anagrafiche/aziende/{self.acme.uid}/", {"name": "Acme", "color": str(0x3D7BEA)})
        self.assertEqual(Company.objects.get(uid=self.acme.uid).color, 0x3D7BEA)
        self.client.post("/anagrafiche/clienti/nuova/", {"name": "Beta srl", "company": str(self.acme.uid), "color": str(0xD64545)})
        self.assertEqual(Client.objects.get(name="Beta srl").color, 0xD64545)
        # outside the app's palette: no colour
        self.client.post(f"/anagrafiche/aziende/{self.acme.uid}/", {"name": "Acme", "color": "123"})
        self.assertIsNone(Company.objects.get(uid=self.acme.uid).color)

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


class PreferencesTests(WorkTests):
    """Dates, times and theme as each user prefers them (issues #38, #39)."""

    def test_preferences_change_the_pages(self):
        self.client.post("/attivita/nuova/", {"when": "past", "client": "22222222-2222-4222-8222-222222222222", "activity": "Riunione",
                                              "start": "2026-10-27T14:05", "end": "2026-10-27T15:30"})
        page = self.client.get("/attivita/?from=2026-10-01").content.decode()
        self.assertIn("14:05–15:30", page)
        self.assertIn('data-theme="system" data-accent="green"', page)
        r = self.client.post("/preferenze/", {"date_format": "us", "time_format": "12", "theme": "dark", "accent": "blue"})
        self.assertRedirects(r, "/preferenze/")
        page = self.client.get("/attivita/?from=2026-10-01").content.decode()
        self.assertIn("2:05 PM–3:30 PM", page)
        self.assertIn('data-theme="dark" data-accent="blue"', page)
        self.assertIn('data-date="us" data-time="12"', page)
        # the forms keep ISO values for the pickers
        e = self.entry()
        self.assertIn('value="2026-10-27T14:05"', self.client.get(f"/attivita/{e.uid}/").content.decode())
        # unknown values are ignored
        self.client.post("/preferenze/", {"date_format": "xx", "time_format": "24", "theme": "neon", "accent": "blue"})
        self.alice.refresh_from_db()
        self.assertEqual((self.alice.date_format, self.alice.time_format, self.alice.theme), ("us", "24", "dark"))

    def test_formats(self):
        from datetime import date

        from web import formats

        token = formats._current.set((formats.ISO, formats.H12))
        try:
            self.assertEqual(formats.date(date(2026, 12, 31)), "2026-12-31")
            self.assertEqual(formats.clock(0), "12:00 AM")
            self.assertEqual(formats.clock(12 * 60 + 5), "12:05 PM")
        finally:
            formats._current.reset(token)
        self.assertEqual(formats.date(date(2026, 12, 31)), "31/12/2026")
        self.assertEqual(formats.clock(9 * 60), "09:00")

    def test_busy_minutes_of_a_day_for_the_grid(self):
        from records.models import Segment

        for start, end in (("2026-10-27T09:00", "2026-10-27T10:30"), ("2026-10-27T14:00", "2026-10-27T15:00")):
            self.client.post("/attivita/nuova/", {"when": "past", "client": "22222222-2222-4222-8222-222222222222", "activity": "x",
                                                  "start": start, "end": end})
        self.assertEqual(self.client.get("/attivita/occupato/?day=2026-10-27").json(), {"busy": [[540, 630], [840, 900]]})
        e = self.entry()
        seg = Segment.objects.get(entry=e)
        self.assertEqual(self.client.get(f"/attivita/occupato/?day=2026-10-27&entry={e.uid}&segment={seg.uid}").json(), {"busy": [[540, 630]]})
        self.assertEqual(self.client.get("/attivita/occupato/?day=x").json(), {"busy": []})
        # another user's day is not shown
        self.client.force_login(self.boss)
        self.assertEqual(self.client.get("/attivita/occupato/?day=2026-10-27").json(), {"busy": []})

    def test_busy_minutes_of_another_only_to_who_fixes_the_times(self):
        from accounts.models import Profile
        from records.models import EntryStatus

        for start, end in (("2026-10-27T09:00", "2026-10-27T10:30"), ("2026-10-27T14:00", "2026-10-27T15:00")):
            self.client.post("/attivita/nuova/", {"when": "past", "client": "22222222-2222-4222-8222-222222222222", "activity": "x",
                                                  "start": start, "end": end})
        e = self.entry()
        self.assertEqual(e.status, EntryStatus.CLOSED)
        url = f"/attivita/occupato/?day=2026-10-27&entry={e.uid}"
        both = {"busy": [[540, 630], [840, 900]]}
        self.assertEqual(self.client.get(url).json(), both)
        # alice's manager fixes her times: her day
        self.client.force_login(self.boss)
        self.assertEqual(self.client.get(url).json(), both)
        # a reader of the company, a manager for the company: the entry, not her day
        reader = User.objects.create_user("acme", password="x-x-x-x-x-x", org=self.org, profile=Profile.READER)
        reader.companies.add(self.acme)
        other = User.objects.create_user("other", password="x-x-x-x-x-x", org=self.org, profile=Profile.MANAGER)
        other.companies.add(self.acme)
        for who in (reader, other):
            self.client.force_login(who)
            self.assertEqual(self.client.get(f"/attivita/{e.uid}/").status_code, 200)
            self.assertEqual(self.client.get(url).json(), {"busy": []})

    def test_company_comes_before_the_client(self):
        page = self.client.get("/attivita/nuova/").content.decode()
        self.assertLess(page.index('name="company"'), page.index('name="client"'))
        self.assertIn(f'data-company="{self.acme.uid}"', page)


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
        self.assertIn("Rinnovo certificati", week)
        self.assertIn("blk todo", week)
        self.assertIn("15:00–16:00", week)
        self.assertIn('data-plan="/dafare/nuova/"', week)
        month = self.client.get("/agenda/?view=month&date=2026-10-27").content.decode()
        self.assertIn("1:15", month)
        self.assertIn('class="dot todo"', month)
        # the last view comes back
        self.assertIn('class="month"', self.client.get("/agenda/").content.decode())

    def test_an_absence_covers_its_working_days(self):
        from records.models import Entry, LunchBreak, Segment, WorkSchedule

        # Wednesday (3) only, with a lunch break
        WorkSchedule.objects.create(org=self.org, owner=None, day_of_week=3, start_min=9 * 60, end_min=17 * 60)
        LunchBreak.objects.create(org=self.org, owner=self.alice, day_of_week=3, start_min=12 * 60 + 30, end_min=13 * 60 + 30)
        # a form of its own: no company, client or due date; the label is optional
        self.assertNotContains(self.client.get("/dafare/nuova/?kind=ABSENCE"), 'name="client"')
        self.client.post("/dafare/nuova/", {"kind": "ABSENCE", "plan_date": "2026-10-27", "plan_end": "2026-10-29",
                                            "client": "22222222-2222-4222-8222-222222222222", "due_date": "2026-11-01"})
        a = Entry.objects.get(kind="ABSENCE")
        self.assertEqual((a.status, a.absence_kind, a.activity, a.company_id, a.client_id, a.due_date), ("CLOSED", "ABSENCE", None, None, None, None))
        segs = list(Segment.objects.filter(entry=a, deleted=False).order_by("start_utc"))
        self.assertEqual([s.kind for s in segs], ["ABSENCE", "ABSENCE"])
        self.assertEqual(a.closed_utc, segs[-1].end_utc)
        week = self.client.get("/agenda/?view=week&date=2026-10-27").content.decode()
        # Tuesday and Thursday have no working hours: one day, cut by the lunch break
        self.assertEqual(week.count("blk todo absence"), 2)
        self.assertIn("09:00–12:30", week)
        self.assertIn("13:30–17:00", week)
        self.assertIn("Assenza", week)
        # absences are not worked time
        from datetime import date

        from web import reporting

        days = reporting.report(
            Entry.objects.filter(owner=self.alice), date(2026, 10, 27), date(2026, 10, 29)
        )
        self.assertEqual([x.reported for d in days.values() for x in d.lines], [])
        self.assertEqual(
            [(d.reported, d.absent) for d in days.values()],
            [(0, (3 * 60 + 30 + 3 * 60 + 30) * 60)]
        )
        # a last day before the first one is refused
        r = self.client.post("/dafare/nuova/", {"kind": "ABSENCE", "plan_date": "2026-11-05", "plan_end": "2026-11-03"})
        self.assertEqual(Entry.objects.filter(kind="ABSENCE").count(), 1)
        self.assertEqual(r.status_code, 200)
        # a label that names a kind; the days as the form shows them (the working ones): the segments stay
        page = self.client.get(f"/dafare/{a.uid}/").content.decode()
        self.assertIn('name="plan_date" value="2026-10-28"', page)
        self.client.post(f"/dafare/{a.uid}/", {"title": "Ferie", "plan_date": "2026-10-28", "plan_end": ""})
        a.refresh_from_db()
        self.assertEqual((a.absence_kind, a.activity), ("HOLIDAY", None))
        self.assertEqual(list(Segment.objects.filter(entry=a, deleted=False).order_by("start_utc")), segs)
        # a "permesso": one day with times
        self.client.post(f"/dafare/{a.uid}/", {"title": "Dentista", "plan_date": "2026-10-28", "plan_time": "15:00", "duration": "90"})
        a.refresh_from_db()
        self.assertEqual((a.absence_kind, a.activity, a.kind), ("HOLIDAY", "Dentista", "ABSENCE"))
        self.assertIn("15:00–16:30", self.client.get("/agenda/?view=day&date=2026-10-28").content.decode())
        page = self.client.get(f"/dafare/{a.uid}/").content.decode()
        self.assertIn('value="15:00"', page)
        self.assertIn('name="end_time" value="16:30"', page)
        # the form's own fields: a kind, an optional company, from-to days, times for every day
        self.client.post("/dafare/nuova/", {
            "kind": "ABSENCE", "absence_kind": "SICK", "company": str(self.acme.uid),
            "plan_date": "2026-11-02",
            "plan_end": "2026-11-03", "plan_time": "09:00", "end_time": "11:30"})
        sick = Entry.objects.get(absence_kind="SICK")
        self.assertEqual(sick.company_id, self.acme.uid)
        self.assertEqual(len(Segment.objects.filter(entry=sick, deleted=False)), 0)
        self.client.post("/dafare/nuova/", {
            "kind": "ABSENCE", "absence_kind": "LEAVE", "plan_date": "2026-10-28",
            "plan_end": "2026-10-28",
            "plan_time": "09:00", "end_time": "08:00"})
        self.assertFalse(Entry.objects.filter(absence_kind="LEAVE").exists())
        # into the bin and back
        self.client.post(f"/dafare/{a.uid}/", {"state": "ignored"})
        self.assertNotIn("blk todo absence", self.client.get("/agenda/?view=day&date=2026-10-28").content.decode())
        self.assertEqual(self.client.get(f"/dafare/{a.uid}/").status_code, 404)
        self.client.post("/cestino/", {"action": "restore", "uid": str(a.uid)})
        self.assertIn(
            "blk todo absence",
            self.client.get("/agenda/?view=day&date=2026-10-28").content.decode()
        )

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
        self.assertTrue(csv.strip().endswith(";75;0;0;0"), csv)


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
        from records.models import Entry

        self.client.post("/dafare/nuova/", {"title": "Avviata", "due_date": "2026-01-01"})
        t = Entry.objects.get(activity="Avviata")
        self.assertIn("Avviata", self.client.get("/").content.decode())
        self.client.post("/attivita/nuova/", {"when": "now", "todo": str(t.uid), "client": "22222222-2222-4222-8222-222222222222"})
        page = self.client.get("/").content.decode()
        self.assertNotIn('<b>Avviata</b>', page)

    def test_displayed_states_of_the_to_dos(self):
        from datetime import date, datetime
        from zoneinfo import ZoneInfo

        from records.models import Entry
        from web import states

        tz = ZoneInfo("Europe/Rome")
        now = datetime(2026, 10, 30, 12, 0, tzinfo=tz)

        def state(**k):
            base = {"kind": "ACTIVITY", "status": "TODO", "activity": "x", "org": self.org,
                    "owner": self.alice, "zone_id": "UTC"}
            e = Entry(**{**base, **k})
            return states.annotate([e], now, tz)[e.uid][0]

        self.assertEqual(state(due_date=date(2026, 10, 30)), "PLANNED")
        self.assertEqual(state(due_date=date(2026, 10, 30), due_time_min=600), "LATE")
        self.assertEqual(state(plan_date=date(2026, 10, 30)), "PLANNED")
        self.assertEqual(
            state(plan_date=date(2026, 10, 30), plan_start_min=600, duration_min=60), "LATE"
        )
        self.assertEqual(
            state(plan_date=date(2026, 10, 30), plan_start_min=660, duration_min=90), "PLANNED"
        )
        self.assertEqual(state(), "TODO")
        self.assertEqual(state(status="CLOSED"), "DONE")
        self.assertEqual(state(deleted_utc=now), "DELETED")
        self.assertEqual(state(kind="ABSENCE", status="CLOSED", absence_kind="SICK"), "ABSENCE")

    def test_the_commands_each_state_allows(self):
        from web import states

        self.assertEqual(
            states.allowed("PLANNED"), ["start_work", "start_travel", "tick", "delete"]
        )
        self.assertNotIn("suspend", sum((states.allowed(s) for s in states.UI), []))
        self.assertEqual(states.allowed("DONE", has_real_segments=False)[-2:], ["untick", "delete"])
        self.assertNotIn("untick", states.allowed("DONE", has_real_segments=True))
        self.assertEqual(states.allowed("DELETED"), ["restore", "purge"])
        self.assertEqual(states.allowed("PAUSE", previous="TRAVEL")[:2], ["resume", "work"])

    def _running(self, mode="WORK"):
        from web import ops

        self.client.post("/attivita/nuova/", {
            "when": "now", "client": "22222222-2222-4222-8222-222222222222", "activity": "Intervento"})
        e = self.entry()
        if mode != "WORK":
            ops.switch(self.alice, e, mode)
        return e

    def test_travel_from_work_asks_first(self):
        from records.models import Entry, Segment

        e = self._running()
        r = self.client.post(f"/attivita/{e.uid}/comando/", {"action": "travel"})
        self.assertRedirects(r, f"/attivita/{e.uid}/?ask=travel", fetch_redirect_response=False)
        self.assertEqual(self.entry().mode, "WORK")
        self.assertContains(
            self.client.get(f"/attivita/{e.uid}/?ask=travel"),
            "Hai completato l'attività lavorativa?"
        )
        # no: the same activity travels
        self.client.post(f"/attivita/{e.uid}/", {"action": "travel_no"})
        self.assertEqual((self.entry().uid, self.entry().mode), (e.uid, "TRAVEL"))
        # yes (from work again): it closes and an anonymous one travels from the same instant
        self.client.post(f"/attivita/{e.uid}/", {"action": "work"})
        self.client.post(f"/attivita/{e.uid}/", {"action": "travel_yes"})
        e.refresh_from_db()
        self.assertEqual(e.status, "CLOSED")
        new = Entry.objects.get(owner=self.alice, status="RUNNING")
        self.assertEqual((new.mode, new.company_id, new.client_id), ("TRAVEL", None, None))
        closed = Segment.objects.filter(entry=e, deleted=False).order_by("-start_utc").first()
        self.assertEqual(closed.end_utc, Segment.objects.get(entry=new, deleted=False).start_utc)

    def test_stopping_offers_the_return_trip(self):
        from datetime import timedelta

        from django.utils import timezone

        from records.models import Segment
        from web import ops

        now = timezone.now()
        e = ops.start(
            self.alice, mode="TRAVEL", at=now - timedelta(hours=4), company=self.acme,
            activity="Posa"
        )
        ops.switch(self.alice, e, "WORK", now - timedelta(hours=3))
        out = Segment.objects.get(entry=e, kind="TRAVEL")
        ops.put(self.alice, "segment", out.uid, {"kmDeclared": 12.5})
        # an outbound travel and then work: three choices
        r = self.client.post(f"/attivita/{e.uid}/comando/", {"action": "stop"})
        self.assertRedirects(r, f"/attivita/{e.uid}/?ask=stop", fetch_redirect_response=False)
        page = self.client.get(f"/attivita/{e.uid}/?ask=stop").content.decode()
        for text in ("Avvia viaggio di ritorno", "Termina l'attività",
                     "Termina l'attività ed aggiungi il viaggio di ritorno"):
            self.assertIn(text, page)
        self.client.post(f"/attivita/{e.uid}/", {"action": "stop_travel"})
        self.assertEqual(self.entry().mode, "TRAVEL")
        self.assertEqual(self.entry().status, "RUNNING")
        e = ops.start(
            self.alice, mode="TRAVEL", at=now - timedelta(hours=4), company=self.acme,
            activity="Posa 2"
        )
        ops.switch(self.alice, e, "WORK", now - timedelta(hours=3))
        ops.put(
            self.alice, "segment", Segment.objects.get(entry=e, kind="TRAVEL").uid,
            {"kmDeclared": 12.5}
        )
        self.client.post(f"/attivita/{e.uid}/", {"action": "stop_return"})
        e.refresh_from_db()
        self.assertEqual(e.status, "CLOSED")
        legs = Segment.objects.filter(entry=e, kind="TRAVEL", deleted=False)
        back = legs.order_by("start_utc").last()
        self.assertEqual((back.end_utc - back.start_utc), timedelta(hours=1))
        self.assertEqual((back.km_declared, back.source), (12.5, "MANUAL"))
        # no travel before the work: a plain stop, no question
        f = self._running()
        r = self.client.post(f"/attivita/{f.uid}/comando/", {"action": "stop"})
        self.assertRedirects(r, "/")

    def test_the_return_trip_needs_editable_times(self):
        from datetime import timedelta

        from django.utils import timezone

        from web import ops

        now = timezone.now()
        e = ops.start(self.alice, mode="TRAVEL", at=now - timedelta(hours=4), company=self.acme)
        ops.switch(self.alice, e, "WORK", now - timedelta(hours=3))
        from unittest import mock

        with mock.patch("web.data.owner_times_fixed", return_value=True):
            page = self.client.get(f"/attivita/{e.uid}/?ask=stop").content.decode()
            self.client.post(f"/attivita/{e.uid}/", {"action": "stop_return"})
        self.assertNotIn("aggiungi il viaggio di ritorno", page)
        self.assertEqual(self.entry().status, "RUNNING")

    def test_the_bin_is_the_only_place_to_delete_for_good(self):
        from records.models import Entry

        e = self._running()
        self.client.post(f"/attivita/{e.uid}/", {"action": "delete"})
        self.assertIsNotNone(Entry.objects.get(uid=e.uid).deleted_utc)
        self.assertContains(self.client.get("/cestino/"), "Intervento")
        self.assertContains(self.client.get("/cestino/"), "Elimina definitivamente")
        # a manager reads the team's bin but changes nothing in it
        self.client.force_login(self.boss)
        self.assertContains(self.client.get("/cestino/"), "Intervento")
        self.assertEqual(
            self.client.post("/cestino/", {"action": "purge", "uid": str(e.uid)}).status_code, 403
        )
        self.assertFalse(Entry.objects.get(uid=e.uid).deleted)
        # nothing is purged out of the bin
        from web import ops

        self.client.force_login(self.alice)
        self.client.post("/cestino/", {"action": "restore", "uid": str(e.uid)})
        with self.assertRaises(ops.OpError):
            ops.purge(self.alice, Entry.objects.get(uid=e.uid))

    def test_a_closed_task_can_be_ticked_off_again_and_reopened_with_confirmation(self):
        from records.models import Entry

        self.client.post("/dafare/nuova/", {"title": "Chiamare"})
        c = Entry.objects.get(activity="Chiamare")
        self.client.post(f"/dafare/{c.uid}/", {"action": "tick"})
        page = self.client.get(f"/attivita/{c.uid}/").content.decode()
        self.assertIn('value="untick"', page)
        self.assertEqual(page.count('value="reopen_'), 2)
        self.assertEqual(page.count("data-confirm"), 3)
        self.client.post(f"/attivita/{c.uid}/", {"action": "untick"})
        c.refresh_from_db()
        self.assertEqual((c.status, c.closed_utc), ("TODO", None))
        # a task that has work cannot be unticked
        e = self._running()
        self.client.post(f"/attivita/{e.uid}/", {"action": "stop"})
        self.assertNotIn('value="untick"', self.client.get(f"/attivita/{e.uid}/").content.decode())

    def test_absences_are_counted_apart_in_the_report(self):
        from datetime import date

        from records.models import Entry, WorkSchedule
        from web import reporting

        for dow in (1, 2, 3, 4, 5):
            WorkSchedule.objects.create(
                org=self.org, owner=None, day_of_week=dow, start_min=9 * 60, end_min=13 * 60
            )
        self.client.post("/attivita/nuova/", {
            "when": "past", "client": "22222222-2222-4222-8222-222222222222", "activity": "Riunione",
            "start": "2026-10-27T09:00", "end": "2026-10-27T10:00"})
        self.client.post("/dafare/nuova/", {
            "kind": "ABSENCE", "absence_kind": "HOLIDAY", "plan_date": "2026-10-28",
            "plan_end": "2026-10-29"})
        page = self.client.get("/report/?from=2026-10-01&to=2026-10-31").content.decode()
        self.assertIn("Ferie", page)
        self.assertIn(">8:00<", page)
        # the work total is the work's alone
        self.assertIn("<b>1:00</b>", page)
        csv = self.client.get("/report/csv/?from=2026-10-01&to=2026-10-31").content.decode()
        csv = csv.splitlines()
        self.assertTrue(csv[0].endswith("absence_min"))
        absences = [row.split(";") for row in csv if ";ABSENCE;" in row]
        self.assertEqual(
            [(r[0], r[-1]) for r in absences], [("2026-10-28", "240"), ("2026-10-29", "240")]
        )
        # an absence without a company stays under a company filter
        filtered = self.client.get(f"/report/?from=2026-10-01&to=2026-10-31&company={self.acme.uid}")
        self.assertIn("Ferie", filtered.content.decode())
        days = reporting.report(
            Entry.objects.filter(owner=self.alice), date(2026, 10, 1), date(2026, 10, 31)
        )
        self.assertEqual(sum(d.reported for d in days.values()), 60)

    def test_the_travel_button_starts_an_anonymous_activity(self):
        r = self.client.post("/attivita/viaggio/")
        e = self.entry()
        self.assertRedirects(r, f"/attivita/{e.uid}/")
        self.assertEqual(
            (e.status, e.mode, e.company_id, e.client_id), ("RUNNING", "TRAVEL", None, None)
        )
        page = self.client.get("/").content.decode()
        self.assertIn("st-TRAVEL", page)
        self.assertNotIn("Sospendi", page)

    def test_planned_segments_never_count(self):
        from datetime import date

        from records.models import Entry, Segment
        from web import data, ops, reporting

        self.client.post(
            "/dafare/nuova/",
            {"title": "Posa", "plan_date": "2026-10-27", "plan_time": "09:00", "duration": "60"}
        )
        t = Entry.objects.get(activity="Posa")
        ops.put(self.alice, "segment", ops.new_uid(), {
            "entryUid": str(t.uid), "kind": "WORK", "startUtcMs": 1761555600000,
            "endUtcMs": 1761559200000, "zoneId": "Europe/Rome", "planned": True})
        mine = Entry.objects.filter(owner=self.alice)
        self.assertEqual(reporting.report(mine, date(2026, 10, 1), date(2026, 10, 31)), {})
        self.assertEqual(sum(data.durations(list(Segment.objects.filter(entry=t))).values()), 0)

    def test_priorities_are_a_drop_down_of_labels(self):
        from records.models import Entry

        page = self.client.get("/dafare/nuova/").content.decode()
        for n, name in enumerate(("Nessuna", "Bassa", "Media", "Alta", "Urgente", "Emergenza")):
            self.assertIn(
                f'<option value="{n}" {"selected" if n == 0 else ""}>{name}</option>', page
            )
        self.client.post("/dafare/nuova/", {"title": "Urgenza", "priority": "5"})
        self.assertEqual(Entry.objects.get(activity="Urgenza").priority, 5)
        self.assertIn("Emergenza", self.client.get("/dafare/").content.decode())


class TodoAssignmentTests(WorkTests):
    """A manager gives to-dos to the team (ARCHITECTURE §5): the phones follow the owner."""

    def test_assign_and_reassign(self):
        from records.models import Entry
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
        t = Entry.objects.get(activity="Rinnovo")
        self.assertEqual((t.owner, t.status), (self.alice, "TODO"))
        self.assertEqual(t.assignment.by, self.boss)
        self.assertEqual([x["uid"] for x in engine.pull(self.alice, cursor)["records"] if x["type"] == "entry"], [str(t.uid)])
        self.assertFalse([x for x in engine.pull(self.boss, cursor)["records"] if x["type"] == "entry"])
        # its planned segment follows it
        from web import ops

        seg = ops.new_uid()
        ops.put(self.alice, "segment", seg, {"entryUid": str(t.uid), "kind": "WORK", "startUtcMs": 1761811200000,
                                             "endUtcMs": 1761814800000, "zoneId": "Europe/Rome", "planned": True})
        # the team's list and the person's
        self.assertIn("Rinnovo", self.client.get("/dafare/?person=all").content.decode())
        self.assertNotIn("Rinnovo", self.client.get("/dafare/").content.decode())
        # to carl: alice's phones drop it, carl's get it
        cursor = engine.head(self.org)
        self.client.post(f"/dafare/{t.uid}/", {"title": "Rinnovo", "owner": str(carl.pk), "notes": "urgente"})
        t.refresh_from_db()
        self.assertEqual((t.owner, t.notes), (carl, "urgente"))
        gone = sorted(engine.pull(self.alice, cursor)["gone"], key=lambda x: x["type"])
        self.assertEqual(gone, [{"type": "entry", "uid": str(t.uid)}, {"type": "segment", "uid": seg}])
        self.assertEqual(sorted(x["uid"] for x in engine.pull(carl, cursor)["records"]), sorted([str(t.uid), seg]))
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
        from records.models import Entry

        self.client.force_login(self.boss)
        self.client.post("/dafare/nuova/", {"title": "Chiamata", "owner": str(self.alice.pk)})
        t = Entry.objects.get(activity="Chiamata")
        self.client.post(f"/dafare/{t.uid}/", {"state": "done"})
        t.refresh_from_db()
        self.assertEqual((t.status, t.owner), ("CLOSED", self.alice))
        # a started one is no longer to do: it cannot be given to someone else
        self.client.post("/dafare/nuova/", {"title": "Avviata", "owner": str(self.alice.pk)})
        u = Entry.objects.get(activity="Avviata")
        from web import ops

        ops.start(self.alice, todo=u)
        with self.assertRaises(ops.OpError):
            ops.assign(self.boss, Entry.objects.get(uid=u.uid), self.boss)


class ReviewFixTests(WorkTests):
    """The independent review of 1.0: concurrency on every command, the states' commands, one-day absences, key clashes."""

    def no_concurrency(self):
        from policies.models import Policy

        Policy.objects.create(org=self.org, data={"features": {"concurrentEntries": False}}, revision=1)

    def test_going_back_to_work_suspends_the_other_running_activity(self):
        from web import ops

        a = ops.start(self.alice, activity="A")
        ops.suspend(self.alice, a)
        b = ops.start(self.alice, activity="B")
        self.no_concurrency()
        # resume A: B is suspended
        ops.switch(self.alice, a, ops.WORK)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.mode, b.mode), ("WORK", None))
        # reopen B after closing it: A is suspended
        ops.stop(self.alice, b)
        b.refresh_from_db()
        ops.reopen(self.alice, b)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.status, a.mode, b.status, b.mode), ("RUNNING", None, "RUNNING", "WORK"))
        # a pause makes no room: the other stays as it is
        ops.switch(self.alice, b, ops.PAUSE)
        a.refresh_from_db()
        self.assertEqual(a.mode, None)

    def test_a_command_the_state_does_not_allow_is_refused(self):
        from django.contrib.messages import get_messages

        from web import ops

        e = ops.start(self.alice, activity="A")
        ops.stop(self.alice, e)
        e.refresh_from_db()
        # a stale page posts "pause" to a closed activity
        r = self.client.post(f"/attivita/{e.uid}/", {"action": "pause"})
        self.assertRedirects(r, f"/attivita/{e.uid}/", fetch_redirect_response=False)
        e.refresh_from_db()
        self.assertEqual((e.status, e.mode), ("CLOSED", None))
        self.assertIn("azione non consentita in questo stato", [str(m) for m in get_messages(r.wsgi_request)])
        # what the state allows goes
        self.client.post(f"/attivita/{e.uid}/", {"action": "reopen_work"})
        e.refresh_from_db()
        self.assertEqual((e.status, e.mode), ("RUNNING", "WORK"))
        # an answer of the question page stands for its command: travel, allowed at work
        self.client.post(f"/attivita/{e.uid}/", {"action": "travel_no"})
        e.refresh_from_db()
        self.assertEqual(e.mode, "TRAVEL")

    def test_one_day_with_times_is_kept_on_a_day_without_work(self):
        from datetime import date

        from records.models import Segment, WorkSchedule
        from web import ops

        # Wednesday only; Saturday 31 October has no working hours
        WorkSchedule.objects.create(org=self.org, owner=None, day_of_week=3, start_min=9 * 60, end_min=17 * 60)
        one = ops.save_absence(self.alice, None, first=date(2026, 10, 31), last=date(2026, 10, 31), start_min=600, duration_min=60)
        self.assertEqual(Segment.objects.filter(entry_id=one, deleted=False).count(), 1)
        # over several days the days without work are still skipped
        more = ops.save_absence(self.alice, None, first=date(2026, 10, 27), last=date(2026, 10, 31), start_min=600, duration_min=60)
        self.assertEqual(Segment.objects.filter(entry_id=more, deleted=False).count(), 1)

    def test_assigning_onto_a_key_the_owner_has_is_refused(self):
        from records.models import Entry
        from web import ops

        fields = {"activity": "Rinnovo", "companyUid": str(self.acme.uid), "externalKey": "K-1"}
        mine = Entry.objects.get(uid=ops.save_todo(self.alice, None, fields))
        theirs = Entry.objects.get(uid=ops.save_todo(self.boss, None, fields))
        with self.assertRaises(ops.OpError):
            ops.assign(self.boss, theirs, self.alice)
        theirs.refresh_from_db()
        mine.refresh_from_db()
        self.assertEqual((theirs.owner, mine.owner), (self.boss, self.alice))


    def test_no_real_segments_on_a_todo_or_an_absence(self):
        from datetime import date

        from records.models import Segment
        from web import ops

        todo = ops.save_todo(self.alice, None, {"activity": "Da fare"})
        absence = ops.save_absence(self.alice, None, first=date(2026, 10, 28), last=date(2026, 10, 28))
        before = Segment.objects.filter(entry_id=absence).count()
        for uid in (todo, absence):
            for kind in ("WORK", "TRAVEL", "PAUSE"):
                r = self.client.post(f"/attivita/{uid}/", {"action": "add_segment", "kind": kind,
                                                           "start": "2026-10-28T09:00", "end": "2026-10-28T10:00"})
                self.assertRedirects(r, f"/attivita/{uid}/", fetch_redirect_response=False)
        self.assertEqual(Segment.objects.filter(entry_id=absence).count(), before)
        # the segments of a to-do are its plan (owner, 2026-11-13): stored, all planned
        self.assertEqual(Segment.objects.filter(entry_id=todo, deleted=False, planned=True).count(), 3)
        self.assertFalse(Segment.objects.filter(entry_id=todo, deleted=False, planned=False).exists())
        seg = Segment.objects.filter(entry_id=absence, deleted=False).first()
        if seg:
            self.client.post(f"/attivita/{absence}/", {"action": "segment", "segment": str(seg.uid), "kind": "WORK",
                                                       "start": "2026-10-28T09:00", "end": "2026-10-28T10:00"})
            seg.refresh_from_db()
            self.assertEqual(seg.kind, "ABSENCE")


    def test_a_failed_reassignment_leaves_the_todo_where_it_was(self):
        from records.models import ActivityAssignment, Entry
        from sync import engine

        # the manager's personal client: the assignee cannot see it, so saving it onto the assignee's to-do fails
        engine.apply(self.boss, {"type": "client", "uid": "33333333-3333-4333-8333-333333333333",
                                 "fields": {"name": "Privato", "companyUid": str(self.acme.uid)}})
        self.client.force_login(self.boss)
        self.client.post("/dafare/nuova/", {"title": "Mia", "client": "33333333-3333-4333-8333-333333333333"})
        t = Entry.objects.get(activity="Mia")
        self.assertEqual(t.owner, self.boss)
        r = self.client.post(f"/dafare/{t.uid}/", {"title": "Mia", "owner": str(self.alice.pk), "client": "33333333-3333-4333-8333-333333333333"})
        self.assertEqual(r.status_code, 200)
        t.refresh_from_db()
        self.assertEqual(t.owner, self.boss)
        self.assertFalse(ActivityAssignment.objects.filter(entry=t).exists())


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


class VersionTests(TestCase):
    def test_the_version_is_shown(self):
        from atweb import __version__

        self.assertContains(self.client.get("/login/"), f"ActivityTrack Web {__version__}")
        org = Organization.objects.create(name="Infra")
        self.client.force_login(User.objects.create_user("alice", password="x-x-x-x-x-x", org=org))
        self.assertContains(self.client.get("/"), f"ActivityTrack Web {__version__}")


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

    def test_one_new_email_code_a_minute(self):
        from unittest import mock

        from django.core import mail

        self.alice.mfa_method = "email"
        self.alice.save()
        with self.settings(EMAIL_HOST="smtp.example.com"):
            self.login()
            self.assertEqual(len(mail.outbox), 1)
            r = self.client.post("/login/codice/", {"resend": "1"}, follow=True)
            self.assertContains(r, "Puoi chiedere un nuovo codice tra")
            self.assertEqual(len(mail.outbox), 1)
            later = self.client.session["mfa_pending"]["sent"] + 61
            with mock.patch("web.mfa.time.time", return_value=later):
                self.assertContains(self.client.post("/login/codice/", {"resend": "1"}, follow=True), "Nuovo codice inviato")
                self.assertEqual(len(mail.outbox), 2)
                self.client.post("/login/codice/", {"resend": "1"})
            self.assertEqual(len(mail.outbox), 2)

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


class CspTests(TestCase):
    """Pages carry a Content-Security-Policy and no inline script (docs/SECURITY.md)."""

    def test_pages_have_the_policy_and_no_inline_script(self):
        import re

        from accounts.models import Profile

        org = Organization.objects.create(name="Infra")
        root = User.objects.create_user("root", password="x-x-x-x-x-x", org=org, profile=Profile.ADMIN)
        self.assertIn("script-src 'self'", self.client.get("/login/")["Content-Security-Policy"])
        self.client.force_login(root)
        for url in ["/", "/attivita/", "/attivita/nuova/", "/dafare/", "/agenda/", "/agenda/?view=month", "/report/", "/anagrafiche/", "/dispositivi/",
                    "/utenti/", f"/utenti/{root.pk}/", "/password/", "/sicurezza/", "/policy/", "/policy/org/"]:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertIn("frame-ancestors 'none'", r["Content-Security-Policy"])
            page = r.content.decode()
            self.assertFalse(re.search(r"<script(?![^>]*\bsrc=)", page), url)
            self.assertFalse(re.search(r"\son(click|submit|change|load)=", page), url)


class AdminSiteTests(TestCase):
    """The Django admin: administrators only, through the site's login (web/adminsite.py)."""

    def setUp(self):
        from accounts.models import Profile

        self.org = Organization.objects.create(name="Infra")
        self.root = User.objects.create_user("root", password="x-x-x-x-x-x", org=self.org, profile=Profile.ADMIN)
        self.alice = User.objects.create_user("alice", password="x-x-x-x-x-x", org=self.org)

    def test_the_admin_login_is_the_sites(self):
        r = self.client.get("/admin/")
        self.assertRedirects(r, "/admin/login/?next=/admin/", fetch_redirect_response=False)
        self.assertRedirects(self.client.get(r["Location"]), "/login/?next=/admin/", fetch_redirect_response=False)
        # its form is not there: a password posted to it logs nobody in
        self.client.post("/admin/login/", {"username": "root", "password": "x-x-x-x-x-x", "next": "/admin/"})
        self.assertNotIn("_auth_user_id", self.client.session)
        r = self.client.post("/login/?next=/admin/", {"username": "root", "password": "x-x-x-x-x-x", "next": "/admin/"})
        self.assertRedirects(r, "/admin/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/admin/").status_code, 200)
        # out through the site's logout
        self.assertRedirects(self.client.post("/admin/logout/"), "/login/", fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_an_administrator_with_a_second_step_passes_it_first(self):
        import time

        from web.mfa import new_secret, totp

        self.root.mfa_method, self.root.totp_secret = "totp", new_secret()
        self.root.save()
        r = self.client.post("/login/?next=/admin/", {"username": "root", "password": "x-x-x-x-x-x", "next": "/admin/"})
        self.assertRedirects(r, "/login/codice/", fetch_redirect_response=False)
        self.assertRedirects(self.client.get("/admin/"), "/admin/login/?next=/admin/", fetch_redirect_response=False)
        r = self.client.post("/login/codice/", {"code": totp(self.root.totp_secret, int(time.time() // 30))})
        self.assertRedirects(r, "/admin/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_others_get_no_admin(self):
        from accounts.models import Profile

        self.client.force_login(self.alice)
        self.assertEqual(self.client.get("/admin/login/").status_code, 403)
        self.assertRedirects(self.client.get("/admin/"), "/admin/login/?next=/admin/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/admin/accounts/user/").status_code, 302)
        boss = User.objects.create_user("boss", password="x-x-x-x-x-x", org=self.org, profile=Profile.MANAGER, is_staff=True)
        self.client.force_login(boss)
        self.assertEqual(self.client.get("/admin/login/").status_code, 403)
        self.root.is_active = False
        self.root.save()
        self.client.force_login(self.root)
        self.assertNotEqual(self.client.get("/admin/").status_code, 200)

    def test_the_brake_holds(self):
        for _ in range(10):
            self.client.post("/login/?next=/admin/", {"username": "root", "password": "wrong", "next": "/admin/"})
        r = self.client.post("/login/?next=/admin/", {"username": "root", "password": "x-x-x-x-x-x", "next": "/admin/"})
        self.assertContains(r, "Troppi tentativi")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_an_administrator_profile_manages_the_models(self):
        import re

        self.root.theme, self.root.accent = "dark", "violet"
        self.root.save()
        self.client.force_login(self.root)
        for url in ["/admin/", "/admin/accounts/user/", f"/admin/accounts/user/{self.alice.pk}/change/", "/admin/records/entry/"]:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            page = r.content.decode()
            self.assertFalse(re.search(r"<script(?![^>]*\bsrc=)", page), url)
            self.assertIn('data-theme="dark" data-accent="violet"', page)
            self.assertIn("web/admin.css", page)
            self.assertNotIn("theme-toggle", page)
        # light: Django's dark stylesheet stays out
        self.root.theme = "light"
        self.root.save()
        self.assertNotIn("dark_mode.css", self.client.get("/admin/").content.decode())
        r = self.client.post(f"/admin/accounts/user/{self.alice.pk}/change/", {
            "username": "alice", "first_name": "Alice", "last_name": "", "email": "", "is_active": "on", "profile": "user",
            "org": self.org.pk, "date_joined_0": "2026-01-01", "date_joined_1": "10:00:00", "manager": "", "companies": [],
        })
        self.assertEqual(r.status_code, 302, r.content.decode()[:2000] if r.status_code == 200 else "")
        self.alice.refresh_from_db()
        self.assertEqual(self.alice.first_name, "Alice")


class BadParameterTests(WorkTests):
    """A link or form with an id that is not one: no server error (web/data.py parse_pk, parse_uid)."""

    def test_bad_ids_are_no_server_error(self):
        from accounts.models import Profile

        root = User.objects.create_user("root", password="x-x-x-x-x-x", org=self.org, profile=Profile.ADMIN)
        self.client.force_login(root)
        for url in ["/agenda/?person=abc", "/attivita/?person=abc", "/attivita/?company=xyz", "/report/?person=abc", "/report/?company=xyz",
                    "/report/csv/?person=abc&company=xyz", "/attivita/nuova/?todo=xyz", "/dafare/?person=abc", "/?f=1&company=xyz",
                    "/agenda/?person=%C2%B2", "/report/?person=%C2%B2"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertEqual(self.client.get("/policy/user-abc/").status_code, 404)
        self.assertEqual(self.client.get("/policy/user-%C2%B2/").status_code, 404)
        self.client.force_login(self.alice)
        self.assertEqual(self.client.post("/attivita/nuova/", {"when": "now", "client": "nope", "role": "x", "todo": "xyz"}).status_code, 302)
        e = self.entry()
        for body in ({"action": "segment", "segment": "xyz"}, {"action": "remove_expense", "expense": "xyz"}):
            self.assertEqual(self.client.post(f"/attivita/{e.uid}/", body).status_code, 404, body)
        self.assertEqual(self.client.post("/dafare/nuova/", {"title": "x", "owner": "abc"}).status_code, 403)
        self.assertEqual(self.client.post("/anagrafiche/clienti/nuova/", {"name": "C", "company": "xyz"}).status_code, 302)
        self.client.force_login(root)
        r = self.client.post(f"/utenti/{self.alice.pk}/", {"username": "alice", "profile": "manager", "active": "on", "manager": "abc", "companies": ["xyz"]})
        self.assertEqual(r.status_code, 302)

    def test_csv_cells_are_no_formulas(self):
        from records.models import Client
        from sync import engine

        engine.apply(self.boss, {"type": "client", "uid": "33333333-3333-4333-8333-333333333333", "fields": {"name": "@SUM(1)", "companyUid": str(self.acme.uid)}}, central=True)
        self.assertTrue(Client.objects.filter(name="@SUM(1)").exists())
        self.client.post("/attivita/nuova/", {"when": "past", "client": "33333333-3333-4333-8333-333333333333", "activity": '=HYPERLINK("http://x/";"y")',
                                              "start": "2026-10-27T09:00", "end": "2026-10-27T10:00"})
        self.client.post("/attivita/nuova/", {"when": "past", "client": "22222222-2222-4222-8222-222222222222", "activity": "-1+2",
                                              "start": "2026-10-27T11:00", "end": "2026-10-27T12:00"})
        rows = self.client.get("/report/csv/?from=2026-10-01&to=2026-10-31").content.decode().splitlines()
        body = "\n".join(rows[1:])
        self.assertIn(";'@SUM(1);", body)
        self.assertIn(""";"'=HYPERLINK(""http://x/"";""y"")";""", body)
        self.assertIn(";'-1+2;", body)
        self.assertIn(";Acme;Banca;", body)


class SetupCommandTests(TestCase):
    """manage.py setup: the first administrator's password follows the web's rules."""

    def test_a_weak_password_is_refused(self):
        from io import StringIO
        from unittest import mock

        from django.core.management import CommandError, call_command

        for weak in ("admin", "password123", "1234567890"):
            with self.assertRaises(CommandError) as e:
                call_command("setup", org="Infra", admin="admin", password=weak, stdout=StringIO())
            self.assertTrue(str(e.exception), weak)
        self.assertFalse(Organization.objects.exists())
        with mock.patch("getpass.getpass", return_value="short"), self.assertRaises(CommandError):
            call_command("setup", org="Infra", admin="admin", stdout=StringIO())
        call_command("setup", org="Infra", admin="admin", password="try-it-once", stdout=StringIO())
        self.assertTrue(User.objects.get(username="admin").check_password("try-it-once"))
