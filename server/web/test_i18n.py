# SPDX-License-Identifier: GPL-3.0-or-later
"""The English translation (locale/en): complete, compiled, and what the pages say with it."""

import gettext
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase

from accounts.models import Organization, Profile, User

LOCALE = Path(settings.BASE_DIR) / "locale" / "en" / "LC_MESSAGES"


class CatalogTest(SimpleTestCase):
    """locale/en/LC_MESSAGES/django.po is complete and django.mo, which the server reads, is its compilation."""

    def compile(self):
        if not shutil.which("msgfmt"):
            self.skipTest("msgfmt (the gettext package) is not installed")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        out = os.path.join(tmp, "django.mo")
        r = subprocess.run(
            ["msgfmt", "--check", "--statistics", "-o", out, str(LOCALE / "django.po")], capture_output=True, text=True)
        return r, out

    def test_every_message_is_translated(self):
        r, _ = self.compile()
        self.assertEqual(r.returncode, 0, r.stderr)
        # "460 translated messages." and, when something is left, ", N untranslated messages" or ", N fuzzy translations"
        self.assertRegex(r.stderr, r"^\d+ translated messages\.\s*$", r.stderr)

    def test_the_compiled_catalog_is_the_current_one(self):
        r, out = self.compile()
        committed = LOCALE / "django.mo"
        self.assertTrue(committed.exists(), "run manage.py compilemessages and commit django.mo")
        with open(out, "rb") as a, open(committed, "rb") as b:
            fresh, kept = gettext.GNUTranslations(a), gettext.GNUTranslations(b)
        # the messages, not the header (key ""): the package build normalises its dates
        # (dh_strip_nondeterminism sets PO-Revision-Date to the changelog's), so the
        # installed file differs from the committed one there and nowhere else
        without_header = lambda c: {k: v for k, v in c.items() if k != ""}  # noqa: E731
        self.assertEqual(without_header(fresh._catalog), without_header(kept._catalog), "django.po changed: run manage.py compilemessages")

    def test_no_message_keeps_its_italian_by_accident(self):
        # a catalog entry equal to its msgid is fine for loan words ("Report"), but never for a sentence
        with open(LOCALE / "django.mo", "rb") as f:
            catalog = gettext.GNUTranslations(f)._catalog
        same = [k for k, v in catalog.items() if isinstance(k, str) and k and k == v and len(k) > 30]
        self.assertEqual(same, [])


class EnglishPagesTest(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Infra")
        self.admin = User.objects.create_user("root", password="x", org=self.org, profile=Profile.ADMIN)

    def test_the_login_page_follows_the_browser(self):
        en = self.client.get("/login/", HTTP_ACCEPT_LANGUAGE="en")
        self.assertContains(en, "Log in")
        self.assertContains(en, "Username")
        self.assertContains(en, '<html lang="en"')
        it = self.client.get("/login/", HTTP_ACCEPT_LANGUAGE="it")
        self.assertContains(it, "Accedi")
        self.assertContains(it, "Nome utente")
        self.assertContains(it, '<html lang="it"')

    def test_other_languages_fall_back_to_the_default(self):
        self.assertContains(self.client.get("/login/", HTTP_ACCEPT_LANGUAGE="fr"), "Accedi")

    def test_the_menu_and_the_pages_are_in_english(self):
        self.client.force_login(self.admin)
        for url, words in (
            ("/", ["Master data", "Devices", "Users", "Maps", "Policy"]),
            ("/mappe/", ["Offline maps", "Offer the offline maps to the phones", "No areas. Add one below."]),
            ("/utenti/", ["New user", "Two-step verification required for"]),
            ("/dispositivi/", ["No devices paired.", "Pair a device"]),
            ("/preferenze/", ["Preferences", "Date format", "Appearance", "Language"]),
            ("/policy/", ["App policies", "Scope"]),
        ):
            r = self.client.get(url, HTTP_ACCEPT_LANGUAGE="en")
            self.assertEqual(r.status_code, 200, url)
            for w in words:
                self.assertContains(r, w, msg_prefix=url)

    def test_messages_and_form_errors_are_in_english(self):
        self.client.force_login(self.admin)
        r = self.client.post("/mappe/nuova/", {"name": "", "lat": "99", "lon": "11", "radius_km": "100", "max_zoom": "10"}, HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(r.context["errors"]["lat"], "The latitude goes from -85 to 85.")
        self.assertEqual(r.context["errors"]["name"], "The name is required (80 characters at most).")
        r = self.client.post("/password/", {"old": "x", "new": "y", "again": "z"}, HTTP_ACCEPT_LANGUAGE="en", follow=True)
        self.assertNotContains(r, "La password")

    def test_the_ready_made_areas_have_english_names(self):
        self.client.force_login(self.admin)
        en = self.client.get("/mappe/", HTTP_ACCEPT_LANGUAGE="en")
        self.assertContains(en, "Turin and Piedmont")
        self.assertContains(en, "Reggio Calabria and the Strait")
        self.assertContains(self.client.get("/mappe/", HTTP_ACCEPT_LANGUAGE="it"), "Torino e Piemonte")
        r = self.client.get("/mappe/nuova/?preset=roma", HTTP_ACCEPT_LANGUAGE="en")
        self.assertContains(r, 'value="Rome and Lazio"')

    def test_a_sentence_with_a_link_reads_right_in_english(self):
        self.client.force_login(self.admin)
        r = self.client.get("/utenti/", HTTP_ACCEPT_LANGUAGE="en")
        self.assertContains(r, 'stay in the <a href="/admin/">advanced administration</a>.')

    def test_a_plural_message_agrees_with_its_number(self):
        from django.utils.translation import override, ngettext
        with override("en"):
            self.assertEqual(ngettext("Resta %(n)s codice di recupero.", "Restano %(n)s codici di recupero.", 1) % {"n": 1}, "1 recovery code left.")
            self.assertEqual(ngettext("Resta %(n)s codice di recupero.", "Restano %(n)s codici di recupero.", 5) % {"n": 5}, "5 recovery codes left.")

    def test_the_language_chosen_on_the_preferences_page_is_kept(self):
        self.client.force_login(self.admin)
        r = self.client.post("/i18n/setlang/", {"language": "en", "next": "/preferenze/"}, HTTP_ACCEPT_LANGUAGE="it")
        self.assertRedirects(r, "/preferenze/", fetch_redirect_response=False)
        self.assertEqual(self.client.cookies[settings.LANGUAGE_COOKIE_NAME].value, "en")
        # the cookie wins over the browser
        self.assertContains(self.client.get("/preferenze/", HTTP_ACCEPT_LANGUAGE="it"), "Date format")
        self.client.post("/i18n/setlang/", {"language": "it", "next": "/preferenze/"})
        self.assertContains(self.client.get("/preferenze/", HTTP_ACCEPT_LANGUAGE="en"), "Formato delle date")

    def test_the_language_cannot_send_the_user_elsewhere(self):
        r = self.client.post("/i18n/setlang/", {"language": "en", "next": "https://evil.example/"})
        self.assertNotIn("evil.example", r["Location"])

    def test_an_unknown_language_is_ignored(self):
        self.client.post("/i18n/setlang/", {"language": "xx", "next": "/login/"})
        self.assertNotIn(settings.LANGUAGE_COOKIE_NAME, self.client.cookies)
