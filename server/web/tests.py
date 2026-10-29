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
