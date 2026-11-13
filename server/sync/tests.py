# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import json
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from datetime import timedelta

from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import Device, Organization, PairingCode, Profile, User
from records.models import Company, Contact, Entry, Segment
from sync import compat, engine, wire

T0 = 1761600000000  # 2025-10-27T21:20:00Z


def uid():
    return str(uuid.uuid4())


def entry_fields(**over):
    fields = {
        "status": "RUNNING",
        # no open segment (suspended): mode follows the open segment (issue #55)
        "mode": None,
        "zoneId": "Europe/Rome",
        "activity": "Patching",
        "createdUtcMs": T0,
        "updatedUtcMs": T0,
    }
    fields.update(over)
    return fields


class SyncTestCase(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Infra")
        self.alice = User.objects.create_user("alice", password="x", org=self.org)
        self.bob = User.objects.create_user("bob", password="x", org=self.org)
        self.admin = User.objects.create_user("root", password="x", org=self.org, profile=Profile.ADMIN)
        self.files = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.files)
        # every request as an app of the current schema
        self.client.defaults["HTTP_X_ACTIVITYTRACK_SCHEMA"] = str(compat.SCHEMA)

    def pair(self, user, name="Telefono", **more):
        _, code = PairingCode.issue(user)
        body = {"code": code, "deviceName": name, **more}
        r = self.client.post("/api/v1/pair", json.dumps(body), content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()["token"]

    def push(self, token, changes, device_time=None, schema=None):
        body = {"changes": changes}
        if device_time is not None:
            body["deviceTime"] = device_time
        headers = {"HTTP_X_ACTIVITYTRACK_SCHEMA": str(schema)} if schema is not None else {}
        r = self.client.post("/api/v1/sync/push", json.dumps(body), content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token}", **headers)
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()["results"]

    def pull(self, token, cursor=0, limit=500):
        r = self.client.get(f"/api/v1/sync/pull?cursor={cursor}&limit={limit}", HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def upsert(self, type_, uid_, fields, t):
        return {"type": type_, "uid": uid_, "op": "upsert", "fields": fields, "times": {k: t for k in fields}}


class PairingTests(SyncTestCase):
    def test_code_works_once(self):
        _, code = PairingCode.issue(self.alice)
        body = json.dumps({"code": code, "deviceName": "Pixel"})
        first = self.client.post("/api/v1/pair", body, content_type="application/json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["user"]["username"], "alice")
        again = self.client.post("/api/v1/pair", body, content_type="application/json")
        self.assertEqual(again.json()["error"], "invalid_code")

    def test_expired_code(self):
        pairing, code = PairingCode.issue(self.alice)
        PairingCode.objects.filter(pk=pairing.pk).update(expires=timezone.now() - timedelta(seconds=1))
        r = self.client.post("/api/v1/pair", json.dumps({"code": code}), content_type="application/json")
        self.assertEqual(r.json()["error"], "invalid_code")

    def test_first_phone_holds_the_plugins(self):
        self.pair(self.alice, "uno")
        self.pair(self.alice, "due")
        holders = [d.name for d in self.alice.devices.filter(plugin_holder=True)]
        self.assertEqual(holders, ["uno"])

    def test_a_desktop_never_holds_the_plugins(self):
        self.pair(self.alice, "pc", kind="desktop")
        self.pair(self.alice, "uno")
        self.pair(self.bob, "pc", kind="desktop")
        self.pair(self.bob, "strano", kind="tablet")
        kinds = {d.name: (d.kind, d.plugin_holder) for d in Device.objects.filter(user=self.alice)}
        self.assertEqual(kinds, {"pc": ("desktop", False), "uno": ("phone", True)})
        self.assertEqual(self.bob.devices.get(name="strano").kind, "phone")
        pc = self.bob.devices.get(name="pc")
        self.client.force_login(self.bob)
        self.assertEqual(self.client.post(f"/dispositivi/{pc.pk}/gestionali/").status_code, 404)

    def test_one_holder_per_user(self):
        first = self.pair(self.alice, "uno")
        second = self.pair(self.alice, "due")
        head = lambda t: self.client.get("/api/v1/sync/head", HTTP_AUTHORIZATION=f"Bearer {t}").json()["pluginHolder"]
        self.assertEqual((head(first), head(second)), (True, False))
        self.client.force_login(self.alice)
        due = self.alice.devices.get(name="due")
        self.client.post(f"/dispositivi/{due.pk}/gestionali/")
        self.assertEqual((head(first), head(second)), (False, True))

    def test_head_names_the_file_limit(self):
        token = self.pair(self.alice)
        r = self.client.get("/api/v1/sync/head", HTTP_AUTHORIZATION=f"Bearer {token}").json()
        self.assertEqual(r["filesMaxMb"], 25)

    def test_revoked_token(self):
        token = self.pair(self.alice)
        self.alice.devices.update(revoked=timezone.now())
        r = self.client.get("/api/v1/sync/head", HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["error"], "revoked")

    def test_reader_cannot_pair(self):
        reader = User.objects.create_user("acme", password="x", org=self.org, profile=Profile.READER)
        _, code = PairingCode.issue(reader)
        r = self.client.post("/api/v1/pair", json.dumps({"code": code}), content_type="application/json")
        self.assertEqual(r.status_code, 403)


class PushPullTests(SyncTestCase):
    def test_entry_reaches_the_other_phone(self):
        phone, tablet = self.pair(self.alice, "phone"), self.pair(self.alice, "tablet")
        e, s = uid(), uid()
        res = self.push(phone, [
            self.upsert("entry", e, entry_fields(mode="WORK"), T0),
            self.upsert("segment", s, {"entryUid": e, "kind": "WORK", "startUtcMs": T0, "zoneId": "Europe/Rome"}, T0),
        ])
        self.assertEqual([r["status"] for r in res], ["applied", "applied"])
        got = self.pull(tablet)
        self.assertEqual({(r["type"], r["uid"]) for r in got["records"]}, {("entry", e), ("segment", s)})
        self.assertFalse(got["more"])
        self.assertEqual(got["records"][1]["fields"]["entryUid"], e)
        self.assertEqual(self.pull(tablet, got["cursor"])["records"], [])

    def test_other_users_data_stays_private(self):
        a, b = self.pair(self.alice), self.pair(self.bob)
        self.push(a, [self.upsert("entry", uid(), entry_fields(), T0)])
        self.assertEqual(self.pull(b)["records"], [])

    def test_later_change_wins_field_by_field(self):
        phone, web = self.pair(self.alice), self.pair(self.alice)
        e = uid()
        self.push(phone, [self.upsert("entry", e, entry_fields(), T0)])
        # the web changes the activity later, the phone the notes and (earlier) the activity
        self.push(web, [self.upsert("entry", e, {"activity": "Web text"}, T0 + 2000)])
        res = self.push(phone, [{"type": "entry", "uid": e, "fields": {"activity": "Phone text", "notes": "n"}, "times": {"activity": T0 + 1000, "notes": T0 + 1000}}])
        self.assertEqual(res[0]["status"], "conflict")
        self.assertEqual(res[0]["refused"], ["activity"])
        self.assertEqual(res[0]["record"]["fields"]["activity"], "Web text")
        self.assertEqual(res[0]["record"]["fields"]["notes"], "n")

    def test_device_clock_offset(self):
        phone, other = self.pair(self.alice), self.pair(self.alice)
        e = uid()
        self.push(other, [self.upsert("entry", e, entry_fields(), T0)])
        now = Entry.objects.get(uid=e).clock["activity"] + 5000
        # a phone one hour behind changes it "now": its time is corrected, so it wins
        behind = now - 3600_000
        res = self.push(phone, [self.upsert("entry", e, {"activity": "late clock"}, behind)], device_time=behind)
        self.assertEqual(res[0]["status"], "applied")

    def test_future_times_are_clamped(self):
        a, b = self.pair(self.alice), self.pair(self.alice)
        e = uid()
        self.push(a, [self.upsert("entry", e, entry_fields(), 99999999999999)])
        self.assertLessEqual(Entry.objects.get(uid=e).clock["activity"], int(timezone.now().timestamp() * 1000))
        res = self.push(b, [self.upsert("entry", e, {"activity": "fixed"}, int(timezone.now().timestamp() * 1000) + 1)])
        self.assertEqual(res[0]["status"], "applied")

    def test_delete_cascades_and_wins(self):
        phone = self.pair(self.alice)
        e, s = uid(), uid()
        self.push(phone, [
            self.upsert("entry", e, entry_fields(mode="WORK"), T0),
            self.upsert("segment", s, {"entryUid": e, "kind": "WORK", "startUtcMs": T0, "zoneId": "Europe/Rome"}, T0),
        ])
        self.push(phone, [{"type": "entry", "uid": e, "op": "delete", "time": T0 + 1}])
        self.assertTrue(Segment.objects.get(uid=s).deleted)
        res = self.push(phone, [self.upsert("entry", e, {"activity": "again"}, T0 + 2)])
        self.assertEqual(res[0]["status"], "conflict")
        self.assertTrue(res[0]["record"]["deleted"])
        records = {r["uid"]: r for r in self.pull(self.pair(self.alice))["records"]}
        self.assertTrue(records[e]["deleted"] and records[s]["deleted"])
        self.assertNotIn("fields", records[s])

    def test_bad_changes_are_reported_one_by_one(self):
        phone = self.pair(self.alice)
        res = self.push(phone, [
            {"type": "spaceship", "uid": uid(), "fields": {}},
            self.upsert("segment", uid(), {"entryUid": uid(), "kind": "WORK", "startUtcMs": T0, "zoneId": "Z"}, T0),
            self.upsert("entry", uid(), entry_fields(status="FLYING"), T0),
            self.upsert("entry", uid(), {"activity": "no status"}, T0),
            self.upsert("entry", uid(), entry_fields(), T0),
        ])
        self.assertEqual([r.get("error") for r in res], ["unknown_type", "unknown_ref", "invalid", "invalid", None])

    def test_another_users_record_is_forbidden(self):
        a, b = self.pair(self.alice), self.pair(self.bob)
        e = uid()
        self.push(a, [self.upsert("entry", e, entry_fields(), T0)])
        res = self.push(b, [self.upsert("entry", e, {"activity": "mine"}, T0 + 1)])
        self.assertEqual(res[0]["error"], "forbidden")

    def test_paging(self):
        phone, tablet = self.pair(self.alice), self.pair(self.alice)
        uids = [uid() for _ in range(5)]
        self.push(phone, [self.upsert("entry", u, entry_fields(), T0) for u in uids])
        seen, cursor = [], 0
        while True:
            page = self.pull(tablet, cursor, limit=2)
            seen += [r["uid"] for r in page["records"]]
            cursor = page["cursor"]
            if not page["more"]:
                break
        self.assertEqual(seen, uids)


class MasterDataTests(SyncTestCase):
    def central_company(self, name="Acme"):
        with transaction.atomic():
            return Company.objects.create(org=self.org, name=name, rev=engine.next_rev(self.org))

    def test_central_records_reach_everyone_read_only(self):
        c = self.central_company()
        a = self.pair(self.alice)
        rec = self.pull(a)["records"][0]
        self.assertTrue(rec["central"])
        res = self.push(a, [self.upsert("company", str(c.uid), {"name": "Mine"}, T0)])
        self.assertEqual(res[0]["error"], "forbidden")
        root = self.pair(self.admin)
        res = self.push(root, [self.upsert("company", str(c.uid), {"name": "Acme S.p.A."}, T0)])
        self.assertEqual(res[0]["status"], "applied")

    def test_a_personal_contact_moves_to_a_central_client(self):
        c = self.central_company()
        central = uid()
        engine.apply(self.admin, self.upsert("client", central, {"companyUid": str(c.uid), "name": "Banca"}, T0), central=True)
        a = self.pair(self.alice)
        mine, ct = uid(), uid()
        self.push(a, [
            self.upsert("client", mine, {"companyUid": str(c.uid), "name": "Banca"}, T0),
            self.upsert("contact", ct, {"clientUid": mine, "name": "Rossi"}, T0),
        ])
        res = self.push(a, [self.upsert("contact", ct, {"clientUid": central}, T0 + 1), {"type": "client", "uid": mine, "op": "delete", "time": T0 + 2}])
        self.assertEqual([r["status"] for r in res], ["applied", "applied"])
        self.assertEqual(res[0]["record"]["fields"]["clientUid"], central)
        self.assertFalse(res[0]["record"]["central"])
        self.assertFalse(Contact.objects.get(uid=ct).deleted)

    def test_only_managers_create_central_records(self):
        with self.assertRaises(engine.ChangeError):
            engine.apply(self.alice, self.upsert("role", uid(), {"name": "Sysadmin"}, T0), central=True)

    def test_phone_companies_are_personal(self):
        a, b = self.pair(self.alice), self.pair(self.bob)
        res = self.push(a, [self.upsert("company", uid(), {"name": "Mia"}, T0)])
        self.assertFalse(res[0]["record"]["central"])
        self.assertEqual(self.pull(b)["records"], [])

    def test_long_texts_are_kept(self):
        # the app has no length limits: a long name from the phone must not be refused
        a = self.pair(self.alice)
        name = "N" * 1000
        res = self.push(a, [self.upsert("company", uid(), {"name": name}, T0)])
        self.assertEqual(res[0]["status"], "applied")
        self.assertEqual(res[0]["record"]["fields"]["name"], name)

    def test_personal_contact_of_a_central_client(self):
        c = self.central_company()
        cl = uid()
        engine.apply(self.admin, self.upsert("client", cl, {"companyUid": str(c.uid), "name": "Banca"}, T0), central=True)
        a, b = self.pair(self.alice), self.pair(self.bob)
        ct = uid()
        res = self.push(a, [self.upsert("contact", ct, {"clientUid": cl, "name": "Rossi", "phones": ["+39055"]}, T0)])
        self.assertFalse(res[0]["record"]["central"])
        self.assertNotIn(ct, [r["uid"] for r in self.pull(b)["records"]])
        self.assertIn(ct, [r["uid"] for r in self.pull(a)["records"]])


@override_settings(FILES_MAX_MB=1)
class FileTests(SyncTestCase):
    def test_upload_and_download(self):
        with self.settings(FILES_DIR=self.files):
            phone, tablet = self.pair(self.alice), self.pair(self.alice)
            e, att = uid(), uid()
            data = b"\x89PNG fake receipt"
            digest = hashlib.sha256(data).hexdigest()
            self.push(phone, [
                self.upsert("entry", e, entry_fields(), T0),
                self.upsert("attachment", att, {"entryUid": e, "kind": "RECEIPT", "mime": "image/png", "createdUtcMs": T0, "sha256": digest}, T0),
            ])
            bad = self.client.put(f"/api/v1/files/{att}", b"other", content_type="image/png", HTTP_AUTHORIZATION=f"Bearer {phone}")
            self.assertEqual(bad.json()["error"], "sha256_mismatch")
            ok = self.client.put(f"/api/v1/files/{att}", data, content_type="image/png", HTTP_AUTHORIZATION=f"Bearer {phone}", HTTP_X_SHA256=digest)
            self.assertEqual(ok.status_code, 200, ok.content)
            self.assertTrue(ok.json()["record"]["fields"]["hasFile"])
            got = self.client.get(f"/api/v1/files/{att}", HTTP_AUTHORIZATION=f"Bearer {tablet}")
            self.assertEqual(b"".join(got.streaming_content), data)
            intruder = self.pair(self.bob)
            self.assertEqual(self.client.get(f"/api/v1/files/{att}", HTTP_AUTHORIZATION=f"Bearer {intruder}").status_code, 404)

    def test_web_server_sends_the_file(self):
        phone = self.pair(self.alice)
        e, att = uid(), uid()
        data = b"pdf"
        self.push(phone, [
            self.upsert("entry", e, entry_fields(), T0),
            self.upsert("attachment", att, {"entryUid": e, "kind": "FILE", "mime": "application/pdf", "createdUtcMs": T0}, T0),
        ])
        with self.settings(FILES_DIR=self.files):
            self.client.put(f"/api/v1/files/{att}", data, content_type="application/pdf", HTTP_AUTHORIZATION=f"Bearer {phone}")
            rel = f"{self.org.pk}/{att[:2]}/{att}"
            with self.settings(FILES_OFFLOAD="nginx", FILES_ACCEL_LOCATION="/_files/"):
                r = self.client.get(f"/api/v1/files/{att}", HTTP_AUTHORIZATION=f"Bearer {phone}")
                self.assertEqual(r["X-Accel-Redirect"], f"/_files/{rel}")
            with self.settings(FILES_OFFLOAD="apache"):
                r = self.client.get(f"/api/v1/files/{att}", HTTP_AUTHORIZATION=f"Bearer {phone}")
                self.assertEqual(r["X-Sendfile"], f"{self.files}/{rel}")
                self.assertEqual(r.content, b"")

    def test_has_file_is_read_only(self):
        phone = self.pair(self.alice)
        e, att = uid(), uid()
        res = self.push(phone, [
            self.upsert("entry", e, entry_fields(), T0),
            self.upsert("attachment", att, {"entryUid": e, "kind": "PHOTO", "mime": "image/jpeg", "createdUtcMs": T0, "hasFile": True}, T0),
        ])
        self.assertEqual(res[1]["refused"], ["hasFile"])
        self.assertFalse(res[1]["record"]["fields"]["hasFile"])


class TagSyncTests(SyncTestCase):
    """Predefined tags (app #133): master data with the name as natural key."""

    def test_names_are_normalised_as_the_app_does(self):
        from records.models import tag_name

        self.assertEqual(tag_name("  #Urgente Remoto! "), "urgente-remoto")
        self.assertEqual(tag_name("Rete_LAN--"), "rete_lan")
        self.assertEqual(tag_name("x" * 40), "x" * 30)
        self.assertEqual(tag_name("#!?"), "")

    def test_a_tag_both_ways_and_central_read_only(self):
        a, b = self.pair(self.alice), self.pair(self.alice, "Altro")
        mine = uid()
        res = self.push(a, [self.upsert("tag", mine, {"name": "#Rete LAN", "color": 0x3D7BEA}, T0)])
        self.assertEqual(res[0]["status"], "applied")
        self.assertEqual(res[0]["record"]["fields"]["name"], "rete-lan")
        got = [r for r in self.pull(b)["records"] if r["type"] == "tag"]
        self.assertEqual([(r["uid"], r["fields"]["color"], r["central"]) for r in got], [(mine, 0x3D7BEA, False)])
        central = uid()
        engine.apply(self.admin, self.upsert("tag", central, {"name": "urgente", "color": 0xD64545}, T0), central=True)
        self.assertIn(central, [r["uid"] for r in self.pull(a)["records"]])
        res = self.push(a, [self.upsert("tag", central, {"color": 0x2E9D4E}, T0 + 1000)])
        self.assertEqual(res[0]["error"], "forbidden")

    def test_one_name_per_owner_and_among_central(self):
        a = self.pair(self.alice)
        self.assertEqual(self.push(a, [self.upsert("tag", uid(), {"name": "rete"}, T0)])[0]["status"], "applied")
        dup = self.push(a, [self.upsert("tag", uid(), {"name": "Rete"}, T0)])[0]
        self.assertEqual((dup["status"], dup["error"], dup["detail"]), ("error", "invalid", "duplicate"))
        # another user's personal tag of the same name is theirs
        self.assertEqual(self.push(self.pair(self.bob), [self.upsert("tag", uid(), {"name": "rete"}, T0)])[0]["status"], "applied")
        engine.apply(self.admin, self.upsert("tag", uid(), {"name": "urgente"}, T0), central=True)
        with self.assertRaises(engine.ChangeError):
            engine.apply(self.admin, self.upsert("tag", uid(), {"name": "urgente"}, T0), central=True)


class LargeFileTests(SyncTestCase):
    """A photo above Django's 2.5 MB body limit goes through; one above files.max_mb does not."""

    def test_four_megabytes(self):
        with self.settings(FILES_DIR=self.files, FILES_MAX_MB=5):
            phone = self.pair(self.alice)
            e, att, big = uid(), uid(), uid()
            data = b"\xff\xd8" + b"x" * (4 * 1024 * 1024)
            self.push(phone, [
                self.upsert("entry", e, entry_fields(), T0),
                self.upsert("attachment", att, {"entryUid": e, "kind": "PHOTO", "mime": "image/jpeg", "createdUtcMs": T0}, T0),
                self.upsert("attachment", big, {"entryUid": e, "kind": "PHOTO", "mime": "image/jpeg", "createdUtcMs": T0}, T0),
            ])
            ok = self.client.put(f"/api/v1/files/{att}", data, content_type="image/jpeg", HTTP_AUTHORIZATION=f"Bearer {phone}",
                                 HTTP_X_SHA256=hashlib.sha256(data).hexdigest())
            self.assertEqual(ok.status_code, 200, ok.content)
            self.assertEqual(ok.json()["record"]["fields"]["size"], len(data))
            too = self.client.put(f"/api/v1/files/{big}", data + b"y" * (2 * 1024 * 1024), content_type="image/jpeg", HTTP_AUTHORIZATION=f"Bearer {phone}")
            self.assertEqual(too.status_code, 413)
            self.assertEqual(sorted(p.name for p in Path(self.files).rglob("*") if p.is_file()), [str(att)])


class LargePushTests(SyncTestCase):
    def test_a_push_above_two_and_a_half_megabytes(self):
        phone = self.pair(self.alice)
        notes = "n" * 8000
        changes = [self.upsert("entry", uid(), {**entry_fields(), "notes": notes}, T0) for _ in range(400)]
        body = json.dumps({"deviceTime": T0, "changes": changes})
        self.assertGreater(len(body), 3 * 1024 * 1024)
        r = self.client.post("/api/v1/sync/push", body, content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {phone}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual({x["status"] for x in r.json()["results"]}, {"applied"})

    def test_a_body_above_the_limit_is_a_413(self):
        token = self.pair(self.alice)
        with self.settings(DATA_UPLOAD_MAX_MEMORY_SIZE=200):
            big = json.dumps({"changes": [], "pad": "x" * 500})
            r = self.client.post("/api/v1/sync/push", big, content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token}")
            self.assertEqual((r.status_code, r.json()["error"]), (413, "too_large"))
            r = self.client.post("/api/v1/pair", big, content_type="application/json")
            self.assertEqual((r.status_code, r.json()["error"]), (413, "too_large"))


class CompatTests(SyncTestCase):
    """Versions of app and server (docs/SYNC.md §12): the schema window and the translations."""

    def get(self, token, path, schema=None, app=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        if schema is not None:
            headers["HTTP_X_ACTIVITYTRACK_SCHEMA"] = str(schema)
        if app:
            headers["HTTP_X_ACTIVITYTRACK_APP"] = app
        return self.client.get(path, **headers)

    def test_window_and_versions(self):
        from accounts.models import Device
        from atweb import __version__

        phone = self.pair(self.alice)
        head = self.get(phone, "/api/v1/sync/head").json()
        self.assertEqual((head["schema"], head["minSchema"], head["server"]), (3, 3, __version__))
        self.get(phone, "/api/v1/sync/head", schema=3, app="1.0.0")
        self.assertEqual(Device.objects.get(user=self.alice).app_version, "1.0.0")
        too_new = self.get(phone, "/api/v1/sync/head", schema=compat.SCHEMA + 1)
        self.assertEqual((too_new.status_code, too_new.json()["error"]), (426, "server_too_old"))
        # pairing refuses too, and the code stays good
        from accounts.models import PairingCode

        _, code = PairingCode.issue(self.alice)
        r = self.client.post("/api/v1/pair", json.dumps({"code": code}), content_type="application/json", HTTP_X_ACTIVITYTRACK_SCHEMA="99")
        self.assertEqual(r.status_code, 426)
        r = self.client.post("/api/v1/pair", json.dumps({"code": code}), content_type="application/json", HTTP_X_ACTIVITYTRACK_SCHEMA="2")
        self.assertEqual((r.status_code, r.json()["error"]), (426, "upgrade_required"))
        r = self.client.post("/api/v1/pair", json.dumps({"code": code, "appVersion": "1.0.0"}), content_type="application/json")
        self.assertEqual((r.status_code, r.json()["schema"]), (200, compat.SCHEMA))

    def test_schema_3_accepts_no_older_app(self):
        """Activity model 1.0: no step reaches back across the to-do's removal (ACTIVITY-MODEL §6)."""
        phone = self.pair(self.alice)
        for schema in (None, 1, 2):
            for path in ("/api/v1/sync/pull?cursor=0", "/api/v1/policy", "/api/v1/maps"):
                headers = {"HTTP_AUTHORIZATION": f"Bearer {phone}"}
                if schema is not None:
                    headers["HTTP_X_ACTIVITYTRACK_SCHEMA"] = str(schema)
                else:
                    # an app without the header speaks schema 1
                    self.client.defaults.pop("HTTP_X_ACTIVITYTRACK_SCHEMA", None)
                r = self.client.get(path, **headers)
                self.assertEqual(r.status_code, 426, (schema, path))
                body = r.json()
                self.assertEqual((body["error"], body["schema"], body["minSchema"]), ("upgrade_required", 3, 3))
                self.assertIn("update the app", body["detail"])
            r = self.client.post("/api/v1/sync/push", json.dumps({"changes": []}), content_type="application/json",
                                 HTTP_AUTHORIZATION=f"Bearer {phone}", **({"HTTP_X_ACTIVITYTRACK_SCHEMA": str(schema)} if schema else {}))
            self.assertEqual((r.status_code, r.json()["error"]), (426, "upgrade_required"))
            # the update offer still reaches it: that is how it learns to update
            r = self.client.get("/api/v1/sync/head", HTTP_AUTHORIZATION=f"Bearer {phone}", **({"HTTP_X_ACTIVITYTRACK_SCHEMA": str(schema)} if schema else {}))
            self.assertEqual((r.status_code, r.json()["minSchema"]), (200, 3))
        self.assertEqual(Device.objects.get(user=self.alice).schema, 2)

    def test_an_older_schema_is_translated_both_ways(self):
        from unittest import mock

        # a later schema 4 that renamed the entry's "activity" to "description", speaking schema 3 too
        def up(change):
            f = dict(change.get("fields") or {})
            if change.get("type") == "entry" and "activity" in f:
                f["description"] = f.pop("activity")
                change = {**change, "fields": f, "times": {("description" if k == "activity" else k): v for k, v in (change.get("times") or {}).items()}}
            return change

        def down(record):
            f = dict(record.get("fields") or {})
            if record.get("type") == "entry" and "description" in f:
                f["activity"] = f.pop("description")
                record = {**record, "fields": f}
            return record

        with mock.patch.object(compat, "SCHEMA", 4), mock.patch.object(compat, "WINDOW", 1), \
                mock.patch.dict(compat.STEPS, {3: compat.Step(up=up, down=down)}):
            phone = self.pair(self.alice)
            e = uid()
            # the schema 3 app writes "activity": the server, in schema 4, reads "description"
            with mock.patch("sync.engine.apply", wraps=__import__("sync.engine", fromlist=["apply"]).apply) as apply:
                self.push(phone, [self.upsert("entry", e, {**entry_fields(), "activity": "Riunione"}, T0)])
                self.assertIn("description", apply.call_args[0][1]["fields"])
            # a record for a schema 3 app comes back with "activity"
            records = self.get(phone, "/api/v1/sync/pull?cursor=0", schema=3).json()["records"]
            entry = next(r for r in records if r["uid"] == e)
            self.assertNotIn("description", entry["fields"])
            # schema 2 is outside the window of a schema 4 server (4 and the one before)
            self.assertEqual(self.get(phone, "/api/v1/sync/pull?cursor=0", schema=2).status_code, 426)

    def test_version_order(self):
        from sync.compat import version_key

        ordered = ["0.9.2", "1.0.0-beta.1", "1.0.0", "1.0.1", "1.1", "1.10.0", "2.0.0-beta.2"]
        self.assertEqual(sorted(reversed(ordered), key=version_key), ordered)
        self.assertEqual(version_key("1.1"), version_key("1.1.0"))


class PolicyCompatTests(SyncTestCase):
    def test_the_editor_warns_of_phones_that_do_not_know_a_key(self):
        from unittest import mock

        from accounts.models import Device
        from policies.models import Policy

        self.pair(self.alice, name="Pixel")
        # a PC ignores the keys it does not use: never in the warning
        self.pair(self.alice, name="Ufficio", kind="desktop")
        Device.objects.filter(user=self.alice).update(app_version="0.9.2")
        Policy.objects.create(org=self.org, data={"settings": {"editableTimes": {"value": False, "locked": True}}})
        self.client.force_login(self.admin)
        self.assertNotContains(self.client.get("/policy/org/"), "rifiutano tutta la policy")
        with mock.patch("policies.catalog.since", side_effect=lambda section, key: "1.1.0" if key == "editableTimes" else "0.9.0"):
            page = self.client.get("/policy/org/").content.decode()
        self.assertIn("rifiutano tutta la policy", page)
        self.assertIn("settings.editableTimes", page)
        self.assertIn("Pixel, 0.9.2", page)
        self.assertNotIn("Ufficio", page)


class JsonShapeTests(SyncTestCase):
    """JSON fields come in their shape (sync/wire.py JSON_SHAPES): a wrong one would break everyone's pages."""

    def entry_with(self, phone, **fields):
        # a closed entry of an hour, so the pages show it
        now = int(time.time() * 1000) - 4 * 3600000
        e = uid()
        res = self.push(phone, [
            self.upsert("entry", e, entry_fields(status="CLOSED", mode=None, createdUtcMs=now, updatedUtcMs=now, **fields), now),
            self.upsert("segment", uid(), {"entryUid": e, "kind": "WORK", "startUtcMs": now, "endUtcMs": now + 3600000, "zoneId": "Europe/Rome"}, now),
        ])
        return e, res[0]

    def test_every_json_field_has_a_shape(self):
        from django.db import models

        from records.models import SYNCED_MODELS

        for m in SYNCED_MODELS:
            for f in m._meta.concrete_fields:
                if isinstance(f, models.JSONField) and f.name not in wire.BOOKKEEPING:
                    self.assertIn(f.name, wire.JSON_SHAPES, f"{m.__name__}.{f.name}")

    def test_wrong_shapes_are_refused(self):
        phone = self.pair(self.alice)
        for bad in (5, "abc", [1, 2], {"a": 1}, True, [["x"]]):
            _, res = self.entry_with(phone, tags=bad)
            self.assertEqual((res["status"], res["error"]), ("error", "invalid"), bad)
        for bad in ([1], "x", 5):
            _, res = self.entry_with(phone, pluginFields=bad)
            self.assertEqual(res.get("error"), "invalid", bad)
        _, res = self.entry_with(phone, pluginFields={"service": "A1"})
        self.assertEqual(res["status"], "applied")
        _, res = self.entry_with(phone, pluginFields=None)
        self.assertEqual(res["status"], "applied")
        cl = uid()
        self.push(phone, [self.upsert("client", cl, {"name": "Banca"}, T0)])
        for bad, error in ((["+39055", 3], "invalid"), ("+39055", "invalid"), (["+39055"], None)):
            res = self.push(phone, [self.upsert("contact", uid(), {"clientUid": cl, "name": "Rossi", "phones": bad}, T0)])[0]
            self.assertEqual(res.get("error"), error, bad)

    def test_tags_are_normalised(self):
        from records.models import Entry

        phone = self.pair(self.alice)
        e, res = self.entry_with(phone, tags=["#Rete LAN", "rete-lan", "Urgente!", "#", "x" * 40])
        self.assertEqual(res["record"]["fields"]["tags"], ["rete-lan", "urgente", "x" * 30])
        self.assertEqual(Entry.objects.get(uid=e).tags, ["rete-lan", "urgente", "x" * 30])

    def test_stored_bad_values_are_repaired(self):
        from io import StringIO

        from django.core.management import call_command

        from records.models import Contact, Entry

        phone = self.pair(self.alice)
        kept = {}
        for i, bad in enumerate((5, "Rete lan", [1, "#Ok"], {"a": 1})):
            e, _ = self.entry_with(phone, tags=[f"t{i}"])
            Entry.objects.filter(uid=e).update(tags=bad, plugin_fields=[1] if i == 0 else None)
            kept[e] = bad
        self.client.force_login(self.admin)
        cl = uid()
        self.push(phone, [self.upsert("client", cl, {"name": "Banca"}, T0)])
        ct = uid()
        self.push(phone, [self.upsert("contact", ct, {"clientUid": cl, "name": "Rossi"}, T0)])
        Contact.objects.filter(uid=ct).update(phones="+39055")
        before = Entry.objects.order_by("-rev").first().rev
        out = StringIO()
        call_command("clean_json_fields", "--dry-run", stdout=out)
        self.assertIn("5 record(s) to repair", out.getvalue())
        self.assertEqual(Entry.objects.filter(uid__in=kept, tags=5).count(), 1)
        call_command("clean_json_fields", stdout=StringIO())
        got = {e.uid: e for e in Entry.objects.filter(uid__in=kept)}
        self.assertEqual(sorted(tuple(e.tags) for e in got.values()), [(), (), ("ok",), ("rete", "lan")])
        self.assertTrue(all(e.plugin_fields is None and e.rev > before for e in got.values()))
        self.assertEqual(Contact.objects.get(uid=ct).phones, ["+39055"])
        alice = self.client.__class__()
        alice.force_login(self.alice)
        for url in ("/", "/attivita/", "/agenda/", "/report/", "/report/csv/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
            self.assertEqual(alice.get(url).status_code, 200, url)
        out = StringIO()
        call_command("clean_json_fields", stdout=out)
        self.assertIn("0 record(s) repaired", out.getvalue())


class ActivityWireTests(SyncTestCase):
    """Wire schema 3 (the app's docs/ACTIVITY-MODEL.md §6.2): the to-do is a state of the entry."""

    ENTRY_NEW = ["kind", "deletedUtcMs", "closedUtcMs", "externalKey", "dueDate", "dueTimeMin", "planDate", "planStartMin",
                 "durationMin", "priority", "absenceKind"]

    def test_the_schema(self):
        types = {t["type"]: t for t in wire.schema()}
        self.assertFalse({"todo", "absence"} & types.keys())
        entry = [f["name"] for f in types["entry"]["fields"]]
        self.assertNotIn("todoUid", entry)
        self.assertEqual(entry[-len(self.ENTRY_NEW):], self.ENTRY_NEW)
        kinds = {f["name"]: (f["kind"], f["null"]) for f in types["entry"]["fields"]}
        self.assertEqual((kinds["priority"], kinds["kind"], kinds["dueDate"], kinds["closedUtcMs"]),
                         (("int", False), ("text", False), ("date", True), ("instant", True)))
        self.assertIn({"name": "planned", "kind": "bool", "null": False}, types["segment"]["fields"])

    def test_sync_schema_command(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("sync_schema", stdout=out)
        doc = json.loads(out.getvalue())
        self.assertEqual(doc["schema"], compat.SCHEMA)
        self.assertEqual([t["type"] for t in doc["types"]], [t["type"] for t in wire.schema()])

    def test_a_todo_with_its_plan_reaches_the_other_phone(self):
        phone, tablet = self.pair(self.alice, "phone"), self.pair(self.alice, "tablet")
        e, s = uid(), uid()
        res = self.push(phone, [
            self.upsert("entry", e, entry_fields(status="TODO", mode=None, kind="ACTIVITY", externalKey="T-1", dueDate="2026-11-20",
                                                 planDate="2026-11-12", planStartMin=540, durationMin=90, priority=4), T0),
            self.upsert("segment", s, {"entryUid": e, "kind": "WORK", "startUtcMs": T0, "endUtcMs": T0 + 3600000, "zoneId": "Europe/Rome",
                                       "planned": True}, T0),
        ])
        self.assertEqual([r["status"] for r in res], ["applied", "applied"])
        got = {r["uid"]: r["fields"] for r in self.pull(tablet)["records"]}
        self.assertEqual((got[e]["status"], got[e]["priority"], got[e]["planDate"], got[e]["externalKey"]), ("TODO", 4, "2026-11-12", "T-1"))
        self.assertTrue(got[s]["planned"])

    def test_values_are_checked(self):
        phone = self.pair(self.alice)
        bad = [
            entry_fields(status="ACTIVE"),
            entry_fields(priority=6),
            entry_fields(priority=None),
            entry_fields(kind="ABSENCE"),
            entry_fields(absenceKind="SICK"),
            entry_fields(kind="ABSENCE", status="CLOSED", mode=None, absenceKind="VACATION"),
        ]
        res = self.push(phone, [self.upsert("entry", uid(), f, T0) for f in bad])
        self.assertEqual([r.get("error") for r in res], ["invalid"] * len(bad))
        e = uid()
        ok = self.push(phone, [self.upsert("entry", e, entry_fields(kind="ABSENCE", status="CLOSED", mode=None, absenceKind="SICK"), T0)])
        self.assertEqual(ok[0]["status"], "applied")
        segs = [
            {"entryUid": e, "kind": "ABSENCE", "startUtcMs": T0, "zoneId": "Europe/Rome"},
            {"entryUid": e, "kind": "ABSENCE", "startUtcMs": T0, "endUtcMs": T0 + 1, "zoneId": "Europe/Rome", "planned": True},
            {"entryUid": e, "kind": "WORK", "startUtcMs": T0, "zoneId": "Europe/Rome", "planned": True},
            {"entryUid": e, "kind": "ABSENCE", "startUtcMs": T0, "endUtcMs": T0 + 1, "zoneId": "Europe/Rome"},
        ]
        res = self.push(phone, [self.upsert("segment", uid(), f, T0) for f in segs])
        self.assertEqual([r.get("error") for r in res], ["invalid", "invalid", "invalid", None])

    def test_the_external_key_is_unique_with_the_company_per_owner(self):
        a, b = self.pair(self.alice), self.pair(self.bob)
        company = uid()
        self.push(a, [self.upsert("company", company, {"name": "Acme"}, T0)])
        todo = dict(entry_fields(status="TODO", mode=None), companyUid=company, externalKey="T-1")
        self.assertEqual(self.push(a, [self.upsert("entry", uid(), todo, T0)])[0]["status"], "applied")
        dup = self.push(a, [self.upsert("entry", uid(), todo, T0)])[0]
        self.assertEqual((dup["error"], dup["detail"]), ("invalid", "duplicate"))
        # without the company, or another person's: another key
        self.assertEqual(self.push(a, [self.upsert("entry", uid(), {**todo, "companyUid": None}, T0)])[0]["status"], "applied")
        self.assertEqual(self.push(b, [self.upsert("entry", uid(), {**todo, "companyUid": None}, T0)])[0]["status"], "applied")

    def test_a_blank_external_key_is_none(self):
        a = self.pair(self.alice)
        todo = dict(entry_fields(status="TODO", mode=None), externalKey="  ")
        first, second = uid(), uid()
        res = self.push(a, [self.upsert("entry", first, todo, T0), self.upsert("entry", second, todo, T0)])
        # no clash between two blanks: neither has a key
        self.assertEqual([r["status"] for r in res], ["applied", "applied"])
        self.assertEqual([Entry.objects.get(uid=u).external_key for u in (first, second)], [None, None])

    def test_the_old_types_are_unknown(self):
        phone = self.pair(self.alice)
        res = self.push(phone, [self.upsert("todo", uid(), {"title": "x"}, T0), self.upsert("absence", uid(), {"date": "2026-11-11"}, T0),
                                self.upsert("entry", uid(), entry_fields(todoUid=uid()), T0)])
        # an unknown field is ignored, as for a newer app
        self.assertEqual([r.get("error") for r in res], ["unknown_type", "unknown_type", None])


H = 3600000


class ActivityInvariantTests(SyncTestCase):
    """
    Issue #55: a push may not leave an activity breaking the model (the app's
    docs/ACTIVITY-MODEL.md §1–3); the commands' own sequences pass in any order.
    """

    def setUp(self):
        super().setUp()
        self.phone = self.pair(self.alice)

    def seg(self, e, kind="WORK", start=T0, end=None, planned=False, t=T0):
        fields = {"entryUid": e, "kind": kind, "startUtcMs": start, "zoneId": "Europe/Rome", "planned": planned}
        if end is not None:
            fields["endUtcMs"] = end
        return fields

    def entry(self, t=T0, **fields):
        return self.upsert("entry", self.e, fields, t)

    def segment(self, s, fields, t=T0):
        return self.upsert("segment", s, fields, t)

    def delete(self, type_, uid_, t):
        return {"type": type_, "uid": uid_, "op": "delete", "time": t}

    def errors(self, res):
        return [(r.get("error"), r.get("detail")) if r["status"] == "error" else r["status"] for r in res]

    def ok(self, changes):
        res = self.push(self.phone, changes)
        self.assertEqual({r["status"] for r in res}, {"applied"}, res)
        return res

    def running(self, mode="WORK"):
        """A running activity with one open segment of [mode]; returns its segment's uid."""
        self.e, s = uid(), uid()
        self.ok([self.entry(**entry_fields(mode=mode)), self.segment(s, self.seg(self.e, mode))])
        return s

    def todo_with_plan(self):
        self.e, p1, p2 = uid(), uid(), uid()
        self.ok([self.entry(**entry_fields(status="TODO", mode=None)),
                 self.segment(p1, self.seg(self.e, start=T0 + 24 * H, end=T0 + 25 * H, planned=True)),
                 self.segment(p2, self.seg(self.e, start=T0 + 26 * H, end=T0 + 27 * H, planned=True))])
        return p1, p2

    def stored(self):
        e = Entry.objects.get(uid=self.e)
        segs = Segment.objects.filter(entry=e, deleted=False)
        return e.status, e.mode, sorted((s.kind, s.end_utc is None, s.planned) for s in segs)

    # ---- a: the kind never changes

    def test_a_kind_never_changes(self):
        self.e = uid()
        self.ok([self.entry(**entry_fields(status="CLOSED"))])
        res = self.push(self.phone, [self.entry(T0 + 1, kind="ABSENCE", absenceKind="SICK", mode=None)])
        self.assertEqual(self.errors(res), [("invalid", "kind_changed")])
        self.assertEqual(Entry.objects.get(uid=self.e).kind, "ACTIVITY")
        # nor back
        self.e = uid()
        self.ok([self.entry(**entry_fields(kind="ABSENCE", status="CLOSED", absenceKind="HOLIDAY"))])
        res = self.push(self.phone, [self.entry(T0 + 1, kind="ACTIVITY", absenceKind=None)])
        self.assertEqual(self.errors(res), [("invalid", "kind_changed")])
        # the same kind again is no change
        self.ok([self.entry(T0 + 2, kind="ABSENCE", notes="ferie")])

    # ---- b: ABSENCE segments in absences only

    def test_segment_kinds_follow_the_activity(self):
        self.e = uid()
        self.ok([self.entry(**entry_fields(status="CLOSED"))])
        absence = uid()
        self.ok([self.upsert("entry", absence, entry_fields(kind="ABSENCE", status="CLOSED", absenceKind="LEAVE"), T0)])
        work = uid()
        res = self.push(self.phone, [
            self.segment(uid(), self.seg(self.e, "ABSENCE", end=T0 + H)),
            self.segment(uid(), self.seg(absence, "WORK", end=T0 + H)),
            self.segment(uid(), self.seg(absence, "PAUSE", end=T0 + H)),
            self.segment(work, self.seg(self.e, "WORK", end=T0 + H)),
        ])
        self.assertEqual(self.errors(res), [("invalid", "segment_kind")] * 3 + ["applied"])
        # nor edited or moved into the other kind of activity
        res = self.push(self.phone, [self.segment(work, {"kind": "ABSENCE"}, T0 + 1)])
        self.assertEqual(self.errors(res), [("invalid", "segment_kind")])
        res = self.push(self.phone, [self.segment(work, {"entryUid": absence}, T0 + 1)])
        self.assertEqual(self.errors(res), [("invalid", "segment_kind")])
        self.assertEqual(Segment.objects.get(uid=work).entry_id, uuid.UUID(self.e))

    # ---- c: a plan only on a TODO activity

    def test_a_plan_only_on_a_todo(self):
        self.e = uid()
        self.ok([self.entry(**entry_fields(status="CLOSED"))])
        res = self.push(self.phone, [self.segment(uid(), self.seg(self.e, end=T0 + H, planned=True))])
        self.assertEqual(self.errors(res), [("invalid", "planned_not_todo")])
        # a real segment made planned
        s = uid()
        self.ok([self.segment(s, self.seg(self.e, end=T0 + H))])
        res = self.push(self.phone, [self.segment(s, {"planned": True}, T0 + 1)])
        self.assertEqual(self.errors(res), [("invalid", "planned_not_todo")])

    def test_leaving_todo_with_the_plan_left_is_refused(self):
        self.todo_with_plan()
        open_ = uid()
        # started, but the planned segments not deleted: the start's changes are refused together
        res = self.push(self.phone, [self.entry(T0 + 1, status="RUNNING", mode="WORK"), self.segment(open_, self.seg(self.e, start=T0 + 1))])
        self.assertEqual(self.errors(res), [("invalid", "planned_not_todo")] * 2)
        self.assertEqual(self.stored(), ("TODO", None, [("WORK", False, True), ("WORK", False, True)]))
        self.assertFalse(Segment.objects.filter(uid=open_).exists())
        res = self.push(self.phone, [self.entry(T0 + 1, status="CLOSED", closedUtcMs=T0 + 1)])
        self.assertEqual(self.errors(res), [("invalid", "planned_not_todo")])

    # ---- d: an absence is always CLOSED

    def test_an_absence_is_always_closed(self):
        self.e = uid()
        res = self.push(self.phone, [self.entry(**entry_fields(kind="ABSENCE", status=status, absenceKind="SICK")) for status in ("TODO", "RUNNING")])
        self.assertEqual(self.errors(res), [("invalid", "absence_not_closed")] * 2)
        self.ok([self.entry(**entry_fields(kind="ABSENCE", status="CLOSED", absenceKind="SICK"))])
        res = self.push(self.phone, [self.entry(T0 + 1, status="TODO")])
        self.assertEqual(self.errors(res), [("invalid", "absence_not_closed")])
        self.assertEqual(Entry.objects.get(uid=self.e).status, "CLOSED")

    # ---- e: mode is the open segment's kind; at most one open segment

    def test_mode_follows_the_open_segment(self):
        self.e = uid()
        res = self.push(self.phone, [self.entry(**entry_fields(mode="WORK"))])
        self.assertEqual(self.errors(res), [("invalid", "mode_mismatch")])
        self.assertFalse(Entry.objects.filter(uid=self.e).exists())
        # an open segment and no mode
        res = self.push(self.phone, [self.entry(**entry_fields(mode=None)), self.segment(uid(), self.seg(self.e))])
        self.assertEqual(self.errors(res), [("invalid", "mode_mismatch")] * 2)
        s = self.running()
        res = self.push(self.phone, [self.entry(T0 + 1, mode="TRAVEL")])
        self.assertEqual(self.errors(res), [("invalid", "mode_mismatch")])
        # the open segment closed, the mode left
        res = self.push(self.phone, [self.segment(s, {"endUtcMs": T0 + H}, T0 + 1)])
        self.assertEqual(self.errors(res), [("invalid", "mode_mismatch")])
        res = self.push(self.phone, [self.delete("segment", s, T0 + 1)])
        self.assertEqual(self.errors(res), [("invalid", "mode_mismatch")])
        self.assertEqual(self.stored(), ("RUNNING", "WORK", [("WORK", True, False)]))

    def test_one_open_segment_at_most(self):
        self.running()
        res = self.push(self.phone, [self.segment(uid(), self.seg(self.e, "WORK", start=T0 + H))])
        self.assertEqual(self.errors(res), [("invalid", "open_segments")])

    def test_only_the_offending_activity_is_refused(self):
        other = uid()
        self.e = uid()
        res = self.push(self.phone, [
            self.entry(**entry_fields(mode="PAUSE")),
            self.upsert("entry", other, entry_fields(status="CLOSED"), T0),
            self.segment(uid(), self.seg(other, end=T0 + H)),
            self.upsert("expense", uid(), {"entryUid": other, "amountCents": 100, "description": "x", "createdUtcMs": T0}, T0),
        ])
        self.assertEqual(self.errors(res), [("invalid", "mode_mismatch"), "applied", "applied", "applied"])

    # ---- the commands' sequences, in any order

    def test_start_with_a_plan_in_either_order(self):
        for entry_first in (True, False):
            p1, p2 = self.todo_with_plan()
            s = uid()
            start = [self.entry(T0 + 1, status="RUNNING", mode="WORK")]
            rest = [self.delete("segment", p1, T0 + 1), self.delete("segment", p2, T0 + 1), self.segment(s, self.seg(self.e, start=T0 + 1), T0 + 1)]
            self.ok(start + rest if entry_first else rest + start)
            self.assertEqual(self.stored(), ("RUNNING", "WORK", [("WORK", True, False)]))
        # the opened segment first, then the entry, then the plan's deletes
        p1, p2 = self.todo_with_plan()
        self.ok([self.segment(uid(), self.seg(self.e, "TRAVEL", start=T0 + 1), T0 + 1), self.entry(T0 + 1, status="RUNNING", mode="TRAVEL"),
                 self.delete("segment", p1, T0 + 1), self.delete("segment", p2, T0 + 1)])
        self.assertEqual(self.stored(), ("RUNNING", "TRAVEL", [("TRAVEL", True, False)]))

    def test_switch_in_any_order(self):
        import itertools

        for order in itertools.permutations(range(3)):
            s = self.running()
            pause = uid()
            changes = [self.segment(s, {"endUtcMs": T0 + H}, T0 + 1), self.segment(pause, self.seg(self.e, "PAUSE", start=T0 + H), T0 + 1),
                       self.entry(T0 + 1, mode="PAUSE")]
            self.ok([changes[i] for i in order])
            self.assertEqual(self.stored(), ("RUNNING", "PAUSE", [("PAUSE", True, False), ("WORK", False, False)]))

    def test_stop_suspend_and_resume(self):
        for entry_first in (True, False):
            s = self.running()
            stop = [self.entry(T0 + 1, status="CLOSED", mode=None, closedUtcMs=T0 + H), self.segment(s, {"endUtcMs": T0 + H}, T0 + 1)]
            self.ok(stop if entry_first else stop[::-1])
            self.assertEqual(self.stored(), ("CLOSED", None, [("WORK", False, False)]))
        # suspended, then resumed travelling
        s = self.running()
        self.ok([self.entry(T0 + 1, mode=None), self.segment(s, {"endUtcMs": T0 + H}, T0 + 1)])
        self.ok([self.segment(uid(), self.seg(self.e, "TRAVEL", start=T0 + 2 * H), T0 + 2), self.entry(T0 + 2, mode="TRAVEL")])
        self.assertEqual(self.stored(), ("RUNNING", "TRAVEL", [("TRAVEL", True, False), ("WORK", False, False)]))

    def test_tick_in_either_order(self):
        for entry_first in (True, False):
            p1, p2 = self.todo_with_plan()
            tick = [self.entry(T0 + 1, status="CLOSED", closedUtcMs=T0 + 1)]
            deletes = [self.delete("segment", p1, T0 + 1), self.delete("segment", p2, T0 + 1)]
            self.ok(tick + deletes if entry_first else deletes + tick)
            self.assertEqual(self.stored(), ("CLOSED", None, []))

    def test_an_absence_is_created(self):
        self.e = uid()
        self.ok([self.entry(**entry_fields(kind="ABSENCE", status="CLOSED", absenceKind="HOLIDAY")),
                 self.segment(uid(), self.seg(self.e, "ABSENCE", start=T0, end=T0 + 4 * H)),
                 self.segment(uid(), self.seg(self.e, "ABSENCE", start=T0 + 5 * H, end=T0 + 9 * H))])
        self.assertEqual(self.stored(), ("CLOSED", None, [("ABSENCE", False, False)] * 2))

    def test_a_running_activity_goes_to_the_bin_as_it_is(self):
        self.running()
        self.ok([self.entry(T0 + 1, deletedUtcMs=T0 + 1)])
        self.assertEqual(self.stored(), ("RUNNING", "WORK", [("WORK", True, False)]))
        self.ok([self.entry(T0 + 2, deletedUtcMs=None)])

    def test_a_todo_is_unticked_and_planned_again(self):
        self.e = uid()
        self.ok([self.entry(**entry_fields(status="CLOSED", closedUtcMs=T0))])
        self.ok([self.entry(T0 + 1, status="TODO", closedUtcMs=None), self.segment(uid(), self.seg(self.e, end=T0 + H, planned=True), T0 + 1)])
        self.assertEqual(self.stored(), ("TODO", None, [("WORK", False, True)]))

    # ---- data stored before the rules (migrated)

    def test_an_old_violation_does_not_block_other_changes(self):
        self.e = uid()
        self.ok([self.entry(**entry_fields(status="CLOSED"))])
        e = Entry.objects.get(uid=self.e)
        planned = Segment.objects.create(org=self.org, owner=self.alice, entry=e, kind="WORK", start_utc=e.created_utc,
                                         end_utc=e.created_utc + timedelta(hours=1), zone_id="Europe/Rome", planned=True)
        Entry.objects.filter(uid=self.e).update(mode="WORK")
        # untouched by the change: notes, the planned segment's note
        self.ok([self.entry(T0 + 1, notes="n"), self.segment(str(planned.uid), {"note": "x"}, T0 + 1)])
        # a change that adds another violation is refused
        res = self.push(self.phone, [self.segment(uid(), self.seg(self.e, start=T0 + 2 * H), T0 + 1),
                                     self.segment(uid(), self.seg(self.e, start=T0 + 3 * H), T0 + 1)])
        self.assertEqual(self.errors(res), [("invalid", "open_segments")] * 2)
        # the repair passes, step by step
        self.ok([self.entry(T0 + 2, mode=None)])
        self.ok([self.delete("segment", str(planned.uid), T0 + 2)])
        self.assertEqual(self.stored(), ("CLOSED", None, []))
        # an absence stored RUNNING may still be edited, and closed
        a = uid()
        Entry.objects.create(org=self.org, owner=self.alice, uid=a, kind="ABSENCE", absence_kind="SICK", status="RUNNING", zone_id="Europe/Rome",
                             created_utc=e.created_utc, updated_utc=e.created_utc)
        self.assertEqual(self.push(self.phone, [self.upsert("entry", a, {"notes": "n"}, T0 + 1)])[0]["status"], "applied")
        self.assertEqual(self.push(self.phone, [self.upsert("entry", a, {"status": "CLOSED"}, T0 + 1)])[0]["status"], "applied")
