# SPDX-License-Identifier: GPL-3.0-or-later
import base64
import datetime
import json
import os
import tempfile

from django.test import TestCase, override_settings

from accounts.models import Organization, PairingCode, Profile, User

from .effective import effective, sign
from .models import Policy


class PolicyTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Infra")
        self.admin = User.objects.create_user("root", password="x", org=self.org, profile=Profile.ADMIN)
        self.alice = User.objects.create_user("alice", password="x", org=self.org)
        self.bob = User.objects.create_user("bob", password="x", org=self.org, profile=Profile.MANAGER)

    def token(self, user):
        _, code = PairingCode.issue(user)
        return self.client.post("/api/v1/pair", json.dumps({"code": code}), content_type="application/json").json()["token"]

    def test_scopes_merge_key_by_key(self):
        Policy.objects.create(org=self.org, data={"features": {"travel": False, "calls": False}, "settings": {"editableTimes": {"value": False, "locked": True}}}, revision=2)
        Policy.objects.create(org=self.org, profile=Profile.USER, data={"features": {"calls": True}, "fields": {"role": {"required": True}}}, revision=1)
        Policy.objects.create(org=self.org, user=self.alice, data={"fields": {"role": {"label": {"it": "Mansione"}}}, "ui": {"language": "it"}}, revision=5)
        p = json.loads(effective(self.alice))
        self.assertEqual(p["format"], "activitytrack-policy")
        self.assertEqual(p["features"], {"travel": False, "calls": True})
        self.assertEqual(p["fields"]["role"], {"required": True, "label": {"it": "Mansione"}})
        self.assertEqual(p["version"], "2.1.5")
        # a manager gets the organisation's only
        self.assertEqual(json.loads(effective(self.bob))["features"], {"travel": False, "calls": False})

    def test_no_policy_anywhere(self):
        self.assertIsNone(effective(self.alice))
        r = self.client.get("/api/v1/policy", HTTP_AUTHORIZATION=f"Bearer {self.token(self.alice)}").json()
        self.assertEqual(r, {"policy": None, "signature": None, "fingerprint": None})

    def test_phone_gets_its_policy(self):
        Policy.objects.create(org=self.org, data={"features": {"travel": False}}, revision=1)
        r = self.client.get("/api/v1/policy", HTTP_AUTHORIZATION=f"Bearer {self.token(self.alice)}").json()
        self.assertEqual(json.loads(r["policy"])["features"], {"travel": False})
        self.assertIsNone(r["signature"])
        self.assertEqual(len(r["fingerprint"]), 64)

    def test_editor_saves_a_valid_body_and_refuses_errors(self):
        self.client.force_login(self.admin)
        form = {
            "feature:travel": "off", "feature:entries": "on",
            "field:role:required": "yes", "field:client:label:it": "Cantiere", "field:client:label:en": "Site",
            "setting:editableTimes:mode": "locked", "setting:editableTimes": "false",
            "setting:roundingWorkMin:mode": "value", "setting:roundingWorkMin": "30",
            "setting:kmFactor:mode": "hidden", "setting:kmFactor": "1,3",
            "setting:homeShow:mode": "value", "setting:homeShow": ["TODO", "MISSING"],
            "behaviour:minDurationSec": "120",
            "masterData": "importOnly", "export": ["csv", "json"], "language": "it",
        }
        r = self.client.post("/policy/org/", form)
        self.assertEqual(r.status_code, 302, r.content.decode()[:500])
        p = Policy.objects.get(org=self.org, profile="", user__isnull=True)
        self.assertEqual(p.revision, 1)
        self.assertEqual(p.data["features"], {"travel": False, "entries": True})
        self.assertEqual(p.data["fields"], {"role": {"required": True}, "client": {"label": {"it": "Cantiere", "en": "Site"}}})
        self.assertEqual(p.data["settings"], {
            "editableTimes": {"value": False, "locked": True},
            "roundingWorkMin": 30,
            "kmFactor": {"value": 1.3, "hidden": True},
            "homeShow": "TODO,MISSING",
        })
        self.assertEqual(p.data["masterData"], {"source": "importOnly"})
        # the app's parser reads this (docs/samples/policy-server-example.json of the app)
        out = os.environ.get("ATWEB_POLICY_SAMPLE")
        if out:
            with open(out, "w") as f:
                f.write(effective(self.alice))
        bad = self.client.post("/policy/org/", {"feature:entries": "off", "setting:roundingWorkMin:mode": "value", "setting:roundingWorkMin": "7"})
        self.assertEqual(bad.status_code, 200)
        self.assertContains(bad, "essenziale")
        self.assertContains(bad, "one of 1, 5, 15, 30, 60")
        self.assertEqual(Policy.objects.get(pk=p.pk).revision, 1)

    def test_deleting_the_last_policy_leaves_an_empty_one(self):
        Policy.objects.create(org=self.org, data={"settings": {"editableTimes": {"value": False, "locked": True}}}, revision=3)
        Policy.objects.create(org=self.org, user=self.alice, data={"ui": {"language": "it"}}, revision=1)
        self.client.force_login(self.admin)
        self.client.post(f"/policy/user-{self.alice.pk}/", {"delete": "1"})
        self.assertFalse(Policy.objects.filter(user=self.alice).exists())
        self.client.post("/policy/org/", {"delete": "1"})
        # the phones get a policy without the lock, not none at all
        p = json.loads(effective(self.alice))
        self.assertNotIn("settings", p)
        self.assertEqual(p["version"], "4")

    def test_only_admins_edit(self):
        self.client.force_login(self.bob)
        self.assertEqual(self.client.get("/policy/").status_code, 403)

    def test_signature_verifies(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Infra policies")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(1) \
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256())
        d = tempfile.mkdtemp()
        kp, cp = os.path.join(d, "k.pem"), os.path.join(d, "c.pem")
        open(kp, "wb").write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        open(cp, "wb").write(cert.public_bytes(serialization.Encoding.PEM))
        Policy.objects.create(org=self.org, data={"features": {"travel": False}}, revision=1)
        with override_settings(POLICY_KEY=kp, POLICY_CERT=cp, POLICY_CHAIN=""):
            text = effective(self.alice)
            sig = json.loads(sign(text))
        self.assertEqual(sig["format"], "activitytrack-policy-signature")
        self.assertEqual(sig["algorithm"], "SHA256withECDSA")
        signer = x509.load_pem_x509_certificate(sig["chain"][0].encode())
        signer.public_key().verify(base64.b64decode(sig["signature"]), text.encode(), ec.ECDSA(hashes.SHA256()))
