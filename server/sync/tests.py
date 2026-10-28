# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import json
import shutil
import tempfile
import uuid
from datetime import timedelta

from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import Organization, PairingCode, Profile, User
from records.models import Company, Entry, Segment
from sync import engine

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

    def pair(self, user, name="Telefono"):
        _, code = PairingCode.issue(user)
        r = self.client.post("/api/v1/pair", json.dumps({"code": code, "deviceName": name}), content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()["token"]

    def push(self, token, changes, device_time=None):
        body = {"changes": changes}
        if device_time is not None:
            body["deviceTime"] = device_time
        r = self.client.post("/api/v1/sync/push", json.dumps(body), content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token}")
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

    def test_only_managers_create_central_records(self):
        with self.assertRaises(engine.ChangeError):
            engine.apply(self.alice, self.upsert("role", uid(), {"name": "Sysadmin"}, T0), central=True)

    def test_phone_companies_are_personal(self):
        a, b = self.pair(self.alice), self.pair(self.bob)
        res = self.push(a, [self.upsert("company", uid(), {"name": "Mia"}, T0)])
        self.assertFalse(res[0]["record"]["central"])
        self.assertEqual(self.pull(b)["records"], [])

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
