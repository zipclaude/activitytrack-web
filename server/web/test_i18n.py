# SPDX-License-Identifier: GPL-3.0-or-later
"""The English translation (locale/en): complete, compiled, and what the pages say with it."""

import ast
import gettext
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from accounts.models import Organization, Profile, User

SERVER = Path(settings.BASE_DIR)
LOCALE = SERVER / "locale"
# the languages of the Android app, the library and the PC interfaces (it, en, de, es): the Italian ids are the
# default, every other one has its catalogue here
LANGUAGES = ("it", "en", "de", "es")
CATALOGUED = tuple(code for code in LANGUAGES if code != settings.LANGUAGE_CODE)
TAGS = re.compile(r"<[^>]+>")
PLACEHOLDERS = re.compile(r"%(?:\(\w+\))?[-#0 +]*\d*(?:\.\d+)?[sdif%]")


def po(code):
    return LOCALE / code / "LC_MESSAGES" / "django.po"


def messages_of(code):
    """{catalog key: translation} of the committed django.mo; a plural message has (msgid, n) keys."""
    with open(LOCALE / code / "LC_MESSAGES" / "django.mo", "rb") as f:
        return gettext.GNUTranslations(f)._catalog


class LanguagesTest(SimpleTestCase):
    def test_the_web_speaks_the_languages_of_the_other_apps(self):
        self.assertEqual([code for code, _ in settings.LANGUAGES], list(LANGUAGES))
        self.assertEqual(settings.LANGUAGE_CODE, "it")
        # and the policy editor's labels, which the apps read per language, follow the same list
        from policies.catalog import catalog

        self.assertEqual(sorted(catalog()["languages"]), sorted(LANGUAGES))
        for code in CATALOGUED:
            self.assertTrue(po(code).exists(), f"locale/{code}/LC_MESSAGES/django.po is missing")


class CatalogTest(SimpleTestCase):
    """Each django.po of locale/ is complete; django.mo, which the server reads, is its compilation."""

    def need(self, tool):
        if not shutil.which(tool):
            self.skipTest(f"{tool} (the gettext package) is not installed")

    def compile(self, code):
        self.need("msgfmt")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        out = os.path.join(tmp, "django.mo")
        cmd = ["msgfmt", "--check", "--statistics", "-o", out, str(po(code))]
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r, out

    def test_every_message_is_translated(self):
        for code in CATALOGUED:
            with self.subTest(code):
                r, _ = self.compile(code)
                self.assertEqual(r.returncode, 0, r.stderr)
                # "460 translated messages." and, when something is left, ", N untranslated messages"
                # or ", N fuzzy translations"
                self.assertRegex(r.stderr, r"^\d+ translated messages\.\s*$", r.stderr)

    def test_the_compiled_catalog_is_the_current_one(self):
        for code in CATALOGUED:
            with self.subTest(code):
                r, out = self.compile(code)
                committed = LOCALE / code / "LC_MESSAGES" / "django.mo"
                self.assertTrue(committed.exists(), "run manage.py compilemessages and commit django.mo")
                with open(out, "rb") as a, open(committed, "rb") as b:
                    fresh, kept = gettext.GNUTranslations(a), gettext.GNUTranslations(b)
                # the messages, not the header (key ""): the package build normalises its dates
                # (dh_strip_nondeterminism sets PO-Revision-Date to the changelog's), so the
                # installed file differs from the committed one there and nowhere else
                without_header = lambda c: {k: v for k, v in c.items() if k != ""}  # noqa: E731
                self.assertEqual(without_header(fresh._catalog), without_header(kept._catalog),
                                 f"locale/{code}/django.po changed: run manage.py compilemessages")

    def test_the_catalogues_have_what_the_sources_say_and_nothing_else(self):
        # the ids are extracted from the templates and the code as makemessages does, in a copy of the sources,
        # and compared both ways with each catalogue: a text added or changed without updating them (a missing
        # message, which shows in Italian) and a text dropped or reworded (a stale one) both fail
        self.need("msgcmp")
        self.need("xgettext")
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        skip = shutil.ignore_patterns("__pycache__", "locale", "static", "*.ini", "*.sqlite3")
        shutil.copytree(SERVER, tmp / "src", ignore=skip)
        (tmp / "src" / "locale" / "en" / "LC_MESSAGES").mkdir(parents=True)
        cwd = os.getcwd()
        os.chdir(tmp / "src")
        try:
            with override_settings(LOCALE_PATHS=[tmp / "src" / "locale"]):
                call_command("makemessages", locale=["en"], no_location=True, no_obsolete=True, verbosity=0)
        finally:
            os.chdir(cwd)
        fresh = tmp / "src" / "locale" / "en" / "LC_MESSAGES" / "django.po"
        self.assertTrue(fresh.exists())

        def compare(have, want):
            cmd = ["msgcmp", "--use-untranslated", str(have), str(want)]
            return subprocess.run(cmd, capture_output=True, text=True).stderr

        for code in ("en",) + tuple(c for c in CATALOGUED if c != "en"):
            with self.subTest(code):
                self.assertEqual(compare(po(code), fresh), "",
                                 f"{code}: texts of the sources missing from the catalogue (manage.py makemessages)")
                self.assertEqual(compare(fresh, po(code)), "",
                                 f"{code}: stale texts in the catalogue (manage.py makemessages --no-obsolete)")

    def test_the_languages_have_the_same_messages(self):
        reference = set(messages_of("en"))
        for code in CATALOGUED:
            with self.subTest(code):
                self.assertEqual(set(messages_of(code)), reference)

    def test_translations_keep_the_placeholders_and_the_markup(self):
        for code in CATALOGUED:
            with self.subTest(code):
                for key, text in messages_of(code).items():
                    if not key:
                        continue
                    msgid = key[0] if isinstance(key, tuple) else key
                    for found in (PLACEHOLDERS, TAGS):
                        self.assertEqual(sorted(found.findall(text)), sorted(found.findall(msgid)),
                                         f"{code}: {msgid!r}")

    def test_no_message_keeps_its_italian_by_accident(self):
        # a catalog entry equal to its msgid is fine for loan words ("Report"), but never for a sentence
        for code in CATALOGUED:
            with self.subTest(code):
                same = [k for k, v in messages_of(code).items() if isinstance(k, str) and k and k == v and len(k) > 30]
                self.assertEqual(same, [])


class SourcesTest(SimpleTestCase):
    """What the pages say goes through gettext: no text in the templates or in the operations that skips it."""

    # words that are the same in every language: names, formats, technical terms
    NAMES = {"ActivityTrack", "ActivityTrack Web", "CSV", "km", "· ActivityTrack",
             "© OpenStreetMap contributors · Protomaps"}

    def test_the_templates_have_no_text_outside_the_translation_tags(self):
        found = []
        for f in sorted(SERVER.rglob("*.html")):
            if "locale" in f.parts:
                continue
            text = f.read_text(encoding="utf-8")
            for pattern in (r"\{%\s*blocktranslate.*?endblocktranslate\s*%\}", r"\{#.*?#\}",
                            r"\{%\s*comment.*?endcomment\s*%\}", r"<script.*?</script>", r"<style.*?</style>"):
                text = re.sub(pattern, " ", text, flags=re.S)
            # the visible attributes
            for m in re.finditer(r'\b(?:title|placeholder|aria-label|alt)="([^"{}]*[^\W\d_]{3,}[^"{}]*)"', text):
                found.append(f"{f.relative_to(SERVER)}: attribute {m.group(1)!r}")
            text = re.sub(r"\{%.*?%\}|\{\{.*?\}\}", " ", text, flags=re.S)
            for chunk in TAGS.sub("\x00", text).split("\x00"):
                chunk = chunk.strip()
                # the policy editor shows the keys of the catalog, which are not text
                words = re.search(r"[^\W\d_]{2,}", chunk) and not re.fullmatch(r"\w+\.\w+", chunk)
                if words and chunk not in self.NAMES:
                    found.append(f"{f.relative_to(SERVER)}: {chunk[:60]!r}")
        # admin/*.html explain themselves in comments written as text: they show nothing
        found = [x for x in found if "templates/admin/" not in x]
        self.assertEqual(found, [])

    def test_the_messages_of_the_operations_are_translated(self):
        # an OpError reaches the page as it is (messages.error(str(e))): its text must go through gettext
        bad = []
        for f in ("web/ops.py", "web/work.py", "web/masters.py"):
            tree = ast.parse((SERVER / f).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) == "OpError":
                    arg = node.args[0] if node.args else None
                    # the engine's own errors (code: detail) are passed on as they are
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        bad.append(f"{f}:{node.lineno}: {arg.value!r}")
        self.assertEqual(bad, [])


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


class GermanAndSpanishPagesTest(TestCase):
    """The same pages in the other two languages of the apps (issue #59)."""

    def setUp(self):
        self.org = Organization.objects.create(name="Infra")
        self.admin = User.objects.create_user("root", password="x", org=self.org, profile=Profile.ADMIN)

    def test_the_login_page_follows_the_browser(self):
        for lang, login, user in (("de", "Anmelden", "Benutzername"), ("es", "Iniciar sesión", "Nombre de usuario")):
            r = self.client.get("/login/", HTTP_ACCEPT_LANGUAGE=lang)
            self.assertContains(r, login)
            self.assertContains(r, user)
            self.assertContains(r, f'<html lang="{lang}"')
        # a regional variant of a language it has
        self.assertContains(self.client.get("/login/", HTTP_ACCEPT_LANGUAGE="de-AT,de;q=0.9"), "Anmelden")
        self.assertContains(self.client.get("/login/", HTTP_ACCEPT_LANGUAGE="es-MX"), "Iniciar sesión")

    def test_the_menu_and_the_pages_are_translated(self):
        self.client.force_login(self.admin)
        for lang, words in (
            ("de", {"/": ["Stammdaten", "Geräte", "Benutzer", "Karten", "Richtlinie"],
                    "/preferenze/": ["Datumsformat", "Darstellung", "Sprache"],
                    "/dafare/": ["Aufgaben", "Neue Abwesenheit"], "/agenda/": ["Schon erledigt", "Abwesenheit"]}),
            ("es", {"/": ["Datos maestros", "Dispositivos", "Usuarios", "Mapas", "Política"],
                    "/preferenze/": ["Formato de fecha", "Aspecto", "Idioma"],
                    "/dafare/": ["Pendientes", "Nueva ausencia"], "/agenda/": ["Ya hecha", "Ausencia"]}),
        ):
            for url, expected in words.items():
                r = self.client.get(url, HTTP_ACCEPT_LANGUAGE=lang)
                self.assertEqual(r.status_code, 200, url)
                for w in expected:
                    self.assertContains(r, w, msg_prefix=f"{lang} {url}")

    def test_the_state_and_the_list_called_to_do_differ_as_in_the_app(self):
        from django.utils.translation import gettext, override

        from records import activity as A
        from web import states

        names = (("en", "To do", "To-dos"), ("de", "Zu erledigen", "Aufgaben"), ("es", "Por hacer", "Pendientes"))
        for lang, state, listing in names:
            with override(lang):
                self.assertEqual(str(states.UI[A.TODO][1]), state)
                self.assertEqual(gettext("Da fare"), listing)
        with override("de"):
            self.assertEqual(str(states.UI[A.DONE][1]), "Erledigt")
            self.assertEqual(str(states.UI[A.INCOMPLETE][1]), "Unvollständig")
        with override("es"):
            self.assertEqual(str(states.UI[A.DONE][1]), "Hecha")
            self.assertEqual(str(states.UI[A.INCOMPLETE][1]), "Incompleta")

    def test_a_plural_message_agrees_with_its_number(self):
        from django.utils.translation import ngettext, override

        one, many = "Resta %(n)s codice di recupero.", "Restano %(n)s codici di recupero."
        with override("de"):
            self.assertEqual(ngettext(one, many, 1) % {"n": 1}, "Noch 1 Wiederherstellungscode übrig.")
            self.assertEqual(ngettext(one, many, 5) % {"n": 5}, "Noch 5 Wiederherstellungscodes übrig.")
        with override("es"):
            self.assertEqual(ngettext(one, many, 1) % {"n": 1}, "Queda 1 código de recuperación.")
            self.assertEqual(ngettext(one, many, 5) % {"n": 5}, "Quedan 5 códigos de recuperación.")

    def test_the_language_picker_offers_the_four_languages_and_keeps_the_choice(self):
        self.client.force_login(self.admin)
        page = self.client.get("/preferenze/", HTTP_ACCEPT_LANGUAGE="en").content.decode()
        for code, name in (("it", "Italiano"), ("en", "English"), ("de", "Deutsch"), ("es", "Español")):
            self.assertIn(f'name="language" value="{code}"', page)
            self.assertIn(name, page)
        for lang, word in (("de", "Datumsformat"), ("es", "Formato de fecha")):
            form = {"language": lang, "next": "/preferenze/"}
            r = self.client.post("/i18n/setlang/", form, HTTP_ACCEPT_LANGUAGE="it")
            self.assertRedirects(r, "/preferenze/", fetch_redirect_response=False)
            self.assertEqual(self.client.cookies[settings.LANGUAGE_COOKIE_NAME].value, lang)
            self.assertContains(self.client.get("/preferenze/", HTTP_ACCEPT_LANGUAGE="en"), word)

    def test_messages_and_form_errors_are_translated(self):
        self.client.force_login(self.admin)
        post = {"name": "", "lat": "99", "lon": "11", "radius_km": "100", "max_zoom": "10"}
        r = self.client.post("/mappe/nuova/", post, HTTP_ACCEPT_LANGUAGE="de")
        self.assertEqual(r.context["errors"]["lat"], "Der Breitengrad reicht von -85 bis 85.")
        r = self.client.post("/mappe/nuova/", post, HTTP_ACCEPT_LANGUAGE="es")
        self.assertEqual(r.context["errors"]["lat"], "La latitud va de -85 a 85.")

    def test_a_sentence_with_a_link_keeps_its_link(self):
        self.client.force_login(self.admin)
        for lang, link in (("de", "erweiterten Administration"), ("es", "administración avanzada")):
            r = self.client.get("/utenti/", HTTP_ACCEPT_LANGUAGE=lang)
            self.assertContains(r, f'<a href="/admin/">{link}</a>')
