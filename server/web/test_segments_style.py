# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Flavio Visentin <flavio-activitytrack@infraconsulting.it>
"""The segments of an activity as small rows, as the app's (issue #72)."""

from datetime import datetime, timedelta, timezone as tz
from pathlib import Path

from django.test import TestCase
from django.utils import timezone, translation

from accounts.models import Organization, User
from records.models import Attachment, Entry, Expense, Segment
from sync import engine
from web.templatetags import at

CLIENT = "22222222-2222-4222-8222-222222222222"


class SegmentRowsTests(TestCase):
    def setUp(self):
        translation.activate("it")
        self.addCleanup(translation.deactivate)
        self.org = Organization.objects.create(name="Infra")
        self.boss = User.objects.create_user("boss", password="x-x-x-x-x-x", org=self.org, profile="manager")
        self.alice = User.objects.create_user("alice", password="x-x-x-x-x-x", org=self.org, manager=self.boss)
        engine.apply(self.boss, {"type": "company", "uid": "11111111-1111-4111-8111-111111111111", "fields": {"name": "Acme"}}, central=True)
        engine.apply(self.boss, {"type": "client", "uid": CLIENT, "fields": {
            "name": "Banca", "companyUid": "11111111-1111-4111-8111-111111111111"}}, central=True)
        self.client.force_login(self.alice)
        self.client.post("/attivita/nuova/", {"when": "past", "client": CLIENT, "activity": "Rete",
                                              "start": "2026-10-27T09:00", "end": "2026-10-27T10:30"})
        self.entry = Entry.objects.get(owner=self.alice, activity="Rete")
        self.seg = Segment.objects.get(entry=self.entry, deleted=False)

    def page(self):
        return self.client.get(f"/attivita/{self.entry.uid}/").content.decode()

    def test_a_segment_is_a_row_with_its_badge_times_and_duration(self):
        page = self.page()
        self.assertIn('class="mark st st-WORK"', page)
        self.assertIn("<b>Lavoro</b>", page)
        self.assertIn("1:30", page)
        self.assertNotIn('<table class="list"><thead><tr><th>Tipo', page)

    def test_the_filters_say_what_the_apps_row_says(self):
        today = timezone.localtime().replace(hour=9, minute=0, second=0, microsecond=0)
        open_seg = Segment(entry=self.entry, kind="TRAVEL", start_utc=today, end_utc=None, source="MANUAL", km_declared=12.34)
        self.assertEqual(at.segment_range(open_seg), "09:00 – …")
        self.assertEqual(at.segment_duration(open_seg), "")
        self.assertEqual(at.segment_extra(open_seg), "12.3 km · manuale")
        closed = Segment(entry=self.entry, kind="PAUSE", start_utc=today, end_utc=today + timedelta(minutes=15), planned=True, note="Caffè")
        self.assertEqual(at.segment_range(closed), "09:00 – 09:15")
        self.assertEqual(at.segment_duration(closed), "0:15")
        self.assertEqual(at.segment_extra(closed), "pianificato · Caffè")
        # a day that is not today comes first, and the end names its day when it is another
        past = Segment(entry=self.entry, kind="WORK", start_utc=datetime(2026, 10, 27, 22, 0, tzinfo=tz.utc),
                       end_utc=datetime(2026, 10, 28, 6, 0, tzinfo=tz.utc))
        self.assertRegex(at.segment_range(past), r"^\d.*\d{2}:\d{2} – .*\d{2}:\d{2}$")
        self.assertEqual(at.segment_duration(past), "8:00")

    def test_the_owner_corrects_a_row_opened_like_the_apps_dialog(self):
        page = self.page()
        self.assertIn('<details class="srow">', page)
        self.assertIn('name="action" value="segment"', page)
        self.assertIn('class="seg-edit"', page)

    def test_expenses_and_attachments_are_rows_too(self):
        Expense.objects.create(org=self.org, owner=self.alice, entry=self.entry, description="Pranzo", amount_cents=1280, created_utc=timezone.now())
        Attachment.objects.create(org=self.org, owner=self.alice, entry=self.entry, kind="PHOTO", description="Armadio", created_utc=timezone.now())
        page = self.page()
        self.assertIn("<b>Pranzo</b>", page)
        self.assertIn("12,80", page)
        self.assertIn("Foto · file in arrivo", page)
        self.assertIn("Armadio", page)

    def test_the_stylesheet_has_the_rows_and_room_for_a_date_with_its_time(self):
        css = (Path(__file__).parent / "static/web/app.css").read_text(encoding="utf-8")
        self.assertIn(".srow", css)
        self.assertIn(".picker.dt > input[type=text]", css)
        js = (Path(__file__).parent / "static/web/pickers.js").read_text(encoding="utf-8")
        self.assertIn('" dt"', js)
        # the old table forced the form inline-block: the editing form no longer sits in a table cell
        self.assertNotIn("<td colspan=\"4\"><form", (Path(__file__).parent / "templates/web/entry.html").read_text(encoding="utf-8"))
