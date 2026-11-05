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
from sync import engine, wire

T0 = 1761600000000  # 2025-10-27T21:20:00Z


def uid():
    return str(uuid.uuid4())


def entry_fields(**over):
    fields = {
        "status": "ACTIVE",
        "mode": "WORK",
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
            self.upsert("entry", e, entry_fields(), T0),
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
            self.upsert("entry", e, entry_fields(), T0),
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
        from sync import compat

        phone = self.pair(self.alice)
        # an app without the headers (0.9.x) speaks schema 1
        head = self.get(phone, "/api/v1/sync/head").json()
        self.assertEqual((head["schema"], head["minSchema"], head["server"]), (compat.SCHEMA, compat.min_schema(), __version__))
        self.get(phone, "/api/v1/sync/head", schema=1, app="1.0.0")
        self.assertEqual(Device.objects.get(user=self.alice).app_version, "1.0.0")
        too_new = self.get(phone, "/api/v1/sync/head", schema=compat.SCHEMA + 1)
        self.assertEqual((too_new.status_code, too_new.json()["error"]), (426, "server_too_old"))
        too_old = self.get(phone, "/api/v1/sync/head", schema=0)
        self.assertEqual((too_old.status_code, too_old.json()["error"]), (426, "app_too_old"))
        # pairing refuses too, and the code stays good
        from accounts.models import PairingCode

        _, code = PairingCode.issue(self.alice)
        r = self.client.post("/api/v1/pair", json.dumps({"code": code}), content_type="application/json", HTTP_X_ACTIVITYTRACK_SCHEMA="99")
        self.assertEqual(r.status_code, 426)
        r = self.client.post("/api/v1/pair", json.dumps({"code": code, "appVersion": "1.0.0"}), content_type="application/json", HTTP_X_ACTIVITYTRACK_SCHEMA="1")
        self.assertEqual((r.status_code, r.json()["schema"]), (200, compat.SCHEMA))

    def test_an_older_schema_is_translated_both_ways(self):
        from unittest import mock

        from sync import compat

        # a schema 2 that renamed the entry's "activity" to "description"
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

        with mock.patch.object(compat, "SCHEMA", 2), mock.patch.dict(compat.STEPS, {1: compat.Step(up=up, down=down)}):
            phone = self.pair(self.alice)
            e = uid()
            # the schema 1 app writes "activity": the server, in schema 2, reads "description"
            with mock.patch("sync.engine.apply", wraps=__import__("sync.engine", fromlist=["apply"]).apply) as apply:
                self.push(phone, [self.upsert("entry", e, {**entry_fields(), "activity": "Riunione"}, T0)])
                self.assertIn("description", apply.call_args[0][1]["fields"])
            # a record for a schema 1 app comes back with "activity"
            records = self.get(phone, "/api/v1/sync/pull?cursor=0", schema=1).json()["records"]
            entry = next(r for r in records if r["uid"] == e)
            self.assertNotIn("description", entry["fields"])
            # schema 0 is outside the window of a schema 2 server (2 and the two before)
            self.assertEqual(self.get(phone, "/api/v1/sync/head", schema=0).status_code, 426)

    def test_a_schema_1_app_gets_todos_without_the_absence_fields(self):
        phone = self.pair(self.alice)
        absent, task = uid(), uid()
        fields = {"title": "", "refs": "", "done": False, "createdUtcMs": T0, "updatedUtcMs": T0, "planDate": "2026-10-27"}
        # a schema 2 app writes an absence, an older one reads a to-do titled for it, without the new fields
        self.push(phone, [self.upsert("todo", absent, {**fields, "kind": "ABSENCE", "color": 0xD64545, "planEndDate": "2026-10-29"}, T0)], schema=2)
        self.push(phone, [self.upsert("todo", task, {**fields, "title": "Rinnovo"}, T0)])
        from records.models import Todo

        a = Todo.objects.get(uid=absent)
        self.assertEqual((a.kind, a.color, str(a.plan_end_date)), ("ABSENCE", 0xD64545, "2026-10-29"))
        self.assertEqual(Todo.objects.get(uid=task).kind, "TASK")
        records = {r["uid"]: r for r in self.get(phone, "/api/v1/sync/pull?cursor=0", schema=1).json()["records"]}
        for u in (absent, task):
            self.assertFalse({"kind", "color", "planEndDate"} & records[u]["fields"].keys())
        self.assertEqual(records[absent]["fields"]["title"], "Assenza")
        current = {r["uid"]: r for r in self.get(phone, "/api/v1/sync/pull?cursor=0", schema=2).json()["records"]}
        self.assertEqual((current[absent]["fields"]["kind"], current[absent]["fields"]["planEndDate"]), ("ABSENCE", "2026-10-29"))

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
