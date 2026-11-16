# SPDX-License-Identifier: GPL-3.0-or-later
"""Personal master data made central (issue #70): the rules and what the phones pull."""

from django.utils import translation

from accounts.models import Profile, User
from records.models import Client, Company, Contact, Material, Preset, Role, Tag
from sync import engine
from sync.tests import SyncTestCase

ACME, BANCA, ZETA, ZETA_C, SITE, ROLE, TAG = (f"aaaaaaaa-0000-4000-8000-00000000000{i}" for i in range(1, 8))
CONTACT, PRESET, MATERIAL = "aaaaaaaa-0000-4000-8000-000000000008", "aaaaaaaa-0000-4000-8000-000000000009", "aaaaaaaa-0000-4000-8000-00000000000a"


def uuid_of(text):
    import uuid

    return uuid.UUID(text)


class CentralTests(SyncTestCase):
    def setUp(self):
        super().setUp()
        # the language of the thread is left by whichever test ran before
        translation.activate("it")
        self.addCleanup(translation.deactivate)
        self.boss = User.objects.create_user("boss", password="x-x-x-x-x-x", org=self.org, profile=Profile.MANAGER)
        self.put(self.admin, "company", ZETA_C, {"name": "Zeta"}, central=True)
        self.put(self.boss, "company", ACME, {"name": "Acme", "externalCode": "A1"})
        self.put(self.boss, "client", BANCA, {"name": "Banca", "companyUid": ACME})
        self.put(self.boss, "contact", CONTACT, {"name": "Mario", "clientUid": BANCA})
        self.put(self.boss, "company", ZETA, {"name": "ZETA"})
        self.put(self.boss, "role", ROLE, {"name": "Sistemista"})
        self.put(self.boss, "tag", TAG, {"name": "urgente"})
        self.client.force_login(self.boss)

    def put(self, user, type_, uid, fields, central=False):
        engine.apply(user, {"type": type_, "uid": uid, "fields": fields}, central=central)

    def owner(self, model, uid):
        return model.objects.get(uid=uid).owner_id

    def run_it(self, uid=None):
        url = "/anagrafiche/centrali/" + (f"{uid}/" if uid else "")
        return self.client.post(url, follow=True)

    def test_the_plan_promotes_what_has_no_conflict_and_says_why_not_the_rest(self):
        from web import central

        p = central.plan(self.boss)
        self.assertEqual({central.label(r) for r, _k in p.eligible}, {"Acme", "Sistemista", "urgente", "Banca"})
        (why,) = [w for r, _k, w in p.skipped if r.uid.hex == ZETA.replace("-", "")]
        self.assertIn("Zeta", why)

    def test_a_central_record_of_the_same_code_is_a_conflict_too(self):
        from web import central

        self.put(self.admin, "company", "bbbbbbbb-0000-4000-8000-000000000001", {"name": "Altro", "externalCode": "a1"}, central=True)
        p = central.plan(self.boss)
        self.assertIn("Acme", {central.label(r) for r, _k, _w in p.skipped})
        # the client of a company that stays personal stays personal, and says so
        self.assertIn("Acme", dict((central.label(r), w) for r, _k, w in p.skipped)["Banca"])

    def test_a_personal_record_of_another_user_is_no_conflict(self):
        from web import central

        self.put(self.alice, "company", "bbbbbbbb-0000-4000-8000-000000000002", {"name": "Acme"})
        self.assertIn("Acme", {central.label(r) for r, _k in central.plan(self.boss).eligible})

    def test_promotion_changes_the_owner_in_place_with_parents_first_and_children_along(self):
        before = {m: m.objects.get(uid=u).rev for m, u in ((Company, ACME), (Client, BANCA), (Contact, CONTACT))}
        r = self.run_it()
        self.assertEqual(r.status_code, 200)
        for model, uid in ((Company, ACME), (Client, BANCA), (Contact, CONTACT), (Role, ROLE), (Tag, TAG)):
            self.assertIsNone(self.owner(model, uid), model)
        self.assertEqual(self.owner(Company, ZETA), self.boss.pk)
        revs = [Company.objects.get(uid=ACME).rev, Client.objects.get(uid=BANCA).rev, Contact.objects.get(uid=CONTACT).rev]
        self.assertEqual(revs, sorted(set(revs)))
        self.assertTrue(all(revs[i] > before[m] for i, m in enumerate((Company, Client, Contact))))
        # nothing copied, nothing deleted
        self.assertEqual(Company.objects.filter(org=self.org, name__iexact="acme").count(), 1)
        self.assertEqual(Company.objects.filter(deleted=True).count(), 0)

    def test_the_owners_phone_pulls_it_as_central_and_another_users_phone_as_new(self):
        mine, hers = self.pair(self.boss), self.pair(self.alice)
        cursor = self.pull(mine)["cursor"]
        self.assertEqual({r["uid"] for r in self.pull(hers)["records"]}, {ZETA_C})
        self.run_it()
        pulled = {r["uid"]: r for r in self.pull(mine, cursor)["records"]}
        self.assertTrue(pulled[ACME]["central"] and pulled[BANCA]["central"])
        self.assertFalse(pulled.get(ZETA, {"central": False})["central"])
        order = [r["uid"] for r in self.pull(hers)["records"]]
        self.assertTrue({ACME, BANCA, CONTACT, ROLE, TAG} <= set(order))
        self.assertLess(order.index(ACME), order.index(BANCA))
        self.assertNotIn(ZETA, order)

    def test_the_owner_keeps_editing_what_became_central(self):
        self.run_it()
        mine = self.pair(self.boss)
        (res,) = self.push(mine, [self.upsert("client", BANCA, {"name": "Banca Uno"}, engine.now_ms())])
        self.assertEqual(res["status"], "applied")
        self.assertEqual(Client.objects.get(uid=BANCA).name, "Banca Uno")

    def test_a_second_run_does_nothing_and_a_duplicate_of_the_same_run_waits(self):
        from web import central

        self.put(self.boss, "role", "bbbbbbbb-0000-4000-8000-000000000003", {"name": "sistemista"})
        done = central.promote(self.boss)
        self.assertEqual(len(done.eligible), 4)
        self.assertEqual([central.label(r) for r, _k, _w in done.skipped if isinstance(r, Role)], ["sistemista"])
        revs = sorted(Company.objects.filter(org=self.org).values_list("rev", flat=True))
        again = central.promote(self.boss)
        self.assertEqual(len(again.eligible), 0)
        self.assertEqual(sorted(Company.objects.filter(org=self.org).values_list("rev", flat=True)), revs)

    def test_one_record_whose_company_is_personal_stays_put_and_says_so(self):
        r = self.run_it(BANCA)
        self.assertEqual(self.owner(Client, BANCA), self.boss.pk)
        self.assertContains(r, "rimasta personale")
        # the company first, then the client alone
        self.run_it(ACME)
        self.assertIsNone(self.owner(Company, ACME))
        self.run_it(BANCA)
        self.assertIsNone(self.owner(Client, BANCA))

    def test_presets_and_materials_follow_the_same_rules(self):
        self.put(self.boss, "material", MATERIAL, {"name": "Cavo", "companyUid": ACME})
        self.put(self.boss, "preset", PRESET, {"text": "Assistenza", "companyUid": ACME, "defaultRoleUid": ROLE})
        self.run_it()
        self.assertIsNone(self.owner(Material, MATERIAL))
        self.assertIsNone(self.owner(Preset, PRESET))

    def test_a_plain_user_cannot(self):
        self.client.force_login(self.alice)
        self.put(self.alice, "role", "bbbbbbbb-0000-4000-8000-000000000004", {"name": "Tecnico"})
        self.assertEqual(self.client.get("/anagrafiche/centrali/").status_code, 403)
        self.assertEqual(self.client.post("/anagrafiche/centrali/").status_code, 403)
        self.assertEqual(Role.objects.get(uid="bbbbbbbb-0000-4000-8000-000000000004").owner_id, self.alice.pk)
        self.assertNotContains(self.client.get("/anagrafiche/ruoli/"), "Rendi centrali")

    def test_the_buttons_and_the_preview(self):
        page = self.client.get("/anagrafiche/aziende/")
        self.assertContains(page, "Rendi centrali le mie")
        preview = self.client.get("/anagrafiche/centrali/")
        self.assertContains(preview, "Diventano centrali")
        self.assertContains(preview, "Restano personali")
        self.assertContains(preview, "non si annulla")
        # a GET changes nothing
        self.assertEqual(self.owner(Company, ACME), self.boss.pk)
        self.assertContains(self.client.get(f"/anagrafiche/aziende/{ACME}/"), "Rendi centrale")
        self.run_it()
        self.assertNotContains(self.client.get("/anagrafiche/ruoli/"), "Rendi centrali le mie")
        self.assertNotContains(self.client.get(f"/anagrafiche/aziende/{ACME}/"), "/anagrafiche/centrali/")

    def test_the_archived_stay_personal_and_say_so_when_asked_for_alone(self):
        from web import central

        Company.objects.filter(uid=ACME).update(archived=True)
        p = central.plan(self.boss)
        listed = {central.label(r) for r, _k in p.eligible} | {central.label(r) for r, _k, _w in p.skipped if isinstance(r, Company)}
        self.assertNotIn("Acme", listed)
        # its client waits for it, and the reason is the archive
        self.assertIn("archiviata", dict((central.label(r), w) for r, _k, w in p.skipped)["Banca"])
        (_rec, _k, why), = central.plan(self.boss, uuid_of(ACME)).skipped
        self.assertIn("archiviata", why)
        self.run_it(ACME)
        self.assertEqual(self.owner(Company, ACME), self.boss.pk)
