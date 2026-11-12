# SPDX-License-Identifier: GPL-3.0-or-later
import base64
import datetime
import hashlib
import json
import os
import tempfile
from unittest import mock

import shutil
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.test import TestCase, override_settings

from accounts.models import Organization, PairingCode, Profile, User
from updates import keys

from .effective import effective, fingerprint, sign, sign_for
from .models import Policy, PolicySignature


class PolicyBase(TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.data)
        for o in (override_settings(DATA_DIR=Path(self.data), POLICY_KEY="", POLICY_CERT="", POLICY_CHAIN=""),):
            o.enable()
            self.addCleanup(o.disable)
        self.org = Organization.objects.create(name="Infra")
        self.admin = User.objects.create_user("root", password="x", org=self.org, profile=Profile.ADMIN)
        self.alice = User.objects.create_user("alice", password="x", org=self.org)
        self.bob = User.objects.create_user("bob", password="x", org=self.org, profile=Profile.MANAGER)
        # every request as an app of the current wire schema
        from sync import compat

        self.client.defaults["HTTP_X_ACTIVITYTRACK_SCHEMA"] = str(compat.SCHEMA)

    def token(self, user):
        _, code = PairingCode.issue(user)
        return self.client.post("/api/v1/pair", json.dumps({"code": code}), content_type="application/json").json()["token"]


class PolicyTests(PolicyBase):
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

    def test_the_editor_reads_the_old_name_of_planned_only(self):
        Policy.objects.create(org=self.org, data={"entries": {"source": "todosOnly"}}, revision=1)
        self.client.force_login(self.admin)
        page = self.client.get("/policy/org/").content.decode()
        self.assertIn("<option selected>plannedOnly</option>", page)

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

    def test_update_keys_of_the_apps_0_9_9(self):
        """The app's catalog (docs/policy-catalog.json of its update-manifest branch) has them: the editor offers and checks them."""
        from .catalog import setting, since

        self.assertEqual(("STRING", ["upstream", "server", "custom"]), (setting("updateSource")["kind"], setting("updateSource")["allowed"]))
        for key in ("updateSource", "updateUrl", "updateChannels"):
            self.assertEqual("0.9.9", since("settings", key))
            self.assertEqual("STRING", setting(key)["kind"])
        self.client.force_login(self.admin)
        page = self.client.get("/policy/org/").content.decode()
        for key in ("updateSource", "updateUrl", "updateChannels"):
            self.assertIn(f'name="setting:{key}:mode"', page)
        ok = self.client.post("/policy/org/", {
            "setting:updateSource:mode": "locked", "setting:updateSource": "custom",
            "setting:updateUrl:mode": "locked", "setting:updateUrl": "https://updates.example.com/manifest.json",
            "setting:updateChannels:mode": "value", "setting:updateChannels": "prod,test"})
        self.assertEqual(302, ok.status_code, ok.content.decode()[:300])
        p = Policy.objects.get(org=self.org, profile="", user__isnull=True)
        self.assertEqual({
            "updateSource": {"value": "custom", "locked": True},
            "updateUrl": {"value": "https://updates.example.com/manifest.json", "locked": True},
            "updateChannels": "prod,test"}, p.data["settings"])
        bad = self.client.post("/policy/org/", {"setting:updateSource:mode": "value", "setting:updateSource": "mirror"})
        self.assertContains(bad, "one of upstream, server, custom")

    def test_phones_older_than_0_9_9_are_warned_about_the_update_keys(self):
        from .catalog import unknown_to_phones

        class Phone:
            app_version = "0.9.8"

        found = unknown_to_phones({"settings": {"updateSource": "server", "updateBetas": True}}, [Phone()])
        self.assertEqual([("settings.updateSource", "0.9.9")], [(k, s) for k, s, _d in found])

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


class GeneratedKeyTests(PolicyBase):
    """The server signs the phones' policies with the key it made for itself, for the apps that know it."""

    def fetch(self, token, app=None):
        headers = {"HTTP_X_ACTIVITYTRACK_APP": app} if app else {}
        return self.client.get("/api/v1/policy", HTTP_AUTHORIZATION=f"Bearer {token}", **headers).json()

    def setUp(self):
        super().setUp()
        Policy.objects.create(org=self.org, data={"features": {"travel": False}}, revision=1)

    def test_signed_with_the_generated_key_for_an_app_that_knows_it(self):
        r = self.fetch(self.token(self.alice), app="0.9.9")
        sig = json.loads(r["signature"])
        self.assertEqual(("activitytrack-policy-signature", "SHA256withECDSA"), (sig["format"], sig["algorithm"]))
        leaf = x509.load_pem_x509_certificate(sig["chain"][0].encode())
        leaf.public_key().verify(base64.b64decode(sig["signature"]), r["policy"].encode(), ec.ECDSA(hashes.SHA256()))
        # the leaf is the one of the pairing link's `k`
        self.assertEqual(keys.fingerprint(), hashlib.sha256(leaf.public_bytes(serialization.Encoding.DER)).hexdigest())
        self.assertTrue(leaf.subject.rfc4514_string().startswith("CN=ActivityTrack Web "))

    def test_the_fingerprint_follows_what_was_sent(self):
        signed = self.fetch(self.token(self.alice), app="0.9.9")
        self.assertEqual(fingerprint(signed["policy"], signed["signature"]), signed["fingerprint"])
        old = self.fetch(self.token(self.bob), app="0.9.8")
        self.assertEqual(fingerprint(old["policy"], None), old["fingerprint"])
        self.assertNotEqual(signed["fingerprint"], old["fingerprint"])
        self.assertEqual({"policy", "signature", "fingerprint"}, set(signed))

    def test_an_app_older_than_0_9_9_gets_the_policy_unsigned(self):
        for app in ("0.9.8", "0.9.9-beta1".replace("9-beta1", "8-beta1"), "0.9.4-beta5", None, "garbage"):
            self.assertIsNone(self.fetch(self.token(self.alice), app=app)["signature"], app)
        for app in ("0.9.9", "0.9.10", "1.0.0", "0.9.9-beta1"):
            self.assertTrue(self.fetch(self.token(self.alice), app=app)["signature"], app)

    def test_the_version_is_the_one_of_the_last_request(self):
        token = self.token(self.alice)
        self.assertIsNone(self.fetch(token, app="0.9.8")["signature"])
        self.assertTrue(self.fetch(token, app="0.9.9")["signature"])

    def test_a_configured_key_signs_for_every_app(self):
        from updates.tests import make_signer

        key, _der, fp, cert = make_signer("ec")
        k, c = Path(self.data) / "k.pem", Path(self.data) / "c.pem"
        k.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        c.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        with override_settings(POLICY_KEY=str(k), POLICY_CERT=str(c)):
            for app in ("0.9.8", None, "0.9.9"):
                sig = json.loads(self.fetch(self.token(self.alice), app=app)["signature"])
                self.assertEqual("CN=Test signer", x509.load_pem_x509_certificate(sig["chain"][0].encode()).subject.rfc4514_string())
            self.assertEqual(fp, keys.fingerprint())
        self.assertFalse((Path(self.data) / "signing").exists(), "no key made when one is configured")

    def test_sign_for_without_a_readable_generated_key_leaves_the_policy_unsigned(self):
        with mock.patch.object(keys, "load", side_effect=OSError("read-only")), self.assertLogs("updates", "ERROR"):
            self.assertIsNone(sign_for("{}", "0.9.9"))
        with override_settings(POLICY_KEY="/nope/k.pem", POLICY_CERT="/nope/c.pem"), self.assertRaises(OSError):
            sign("{}")

    def test_the_cn_is_the_same_after_a_restart_and_after_a_new_key(self):
        def cn():
            return x509.load_der_x509_certificate(keys.load()[1][0]).subject.rfc4514_string()

        first, fp = cn(), keys.fingerprint()
        keys._cache.clear()
        self.assertEqual((first, fp), (cn(), keys.fingerprint()), "read again from the files: nothing is regenerated")
        # a key made again (the folder lost) keeps the CN, which is what the phones pinned; its fingerprint is new
        shutil.rmtree(Path(self.data) / "signing")
        keys._cache.clear()
        self.assertEqual(first, cn())
        self.assertNotEqual(fp, keys.fingerprint())
        with override_settings(BASE_URL="https://at.example.com/activitytrack"):
            self.assertEqual("ActivityTrack Web at.example.com", keys.common_name())


class StableSignatureTests(PolicyBase):
    """The app tells a policy it has handled by the hash of its text and signature, whatever the server's own
    `fingerprint` says (PolicyGate.fingerprint): an ECDSA signature differs at every signing, so the one given to a
    text is kept, and every process gives the same."""

    def setUp(self):
        super().setUp()
        Policy.objects.create(org=self.org, data={"features": {"travel": False}}, revision=1)

    def fetch(self, token):
        return self.client.get("/api/v1/policy", HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_X_ACTIVITYTRACK_APP="0.9.9").json()

    def test_the_same_text_has_the_same_signature_at_every_request(self):
        token = self.token(self.alice)
        first = self.fetch(token)
        self.assertTrue(first["signature"])
        for _ in range(3):
            again = self.fetch(token)
            self.assertEqual(first, again)
        self.assertEqual(1, PolicySignature.objects.count())
        # what the app hashes: the text and the signature as sent
        self.assertEqual(fingerprint(first["policy"], first["signature"]), again["fingerprint"])

    def test_without_the_kept_signature_ecdsa_would_differ(self):
        """The reason for the table: signing the same text twice gives two signatures."""
        text = effective(self.alice)
        a = json.loads(sign(text))["signature"]
        PolicySignature.objects.all().delete()
        self.assertNotEqual(a, json.loads(sign(text))["signature"])

    def test_another_process_gives_the_same_signature(self):
        """A worker that starts later reads the same key and the same table: nothing of the first one is in its memory."""
        text = effective(self.alice)
        first = sign(text)
        keys._cache.clear()
        self.assertEqual(first, sign(text))
        self.assertEqual(first, sign_for(text, "0.9.9"))

    def test_a_new_text_or_a_new_key_is_a_new_signature(self):
        from updates.tests import make_signer

        text = effective(self.alice)
        first = sign(text)
        Policy.objects.filter(org=self.org).update(data={"features": {"travel": True}}, revision=2)
        changed = effective(self.alice)
        self.assertNotEqual(text, changed)
        self.assertNotEqual(first, sign(changed))
        key, _der, _fp, cert = make_signer("ec")
        k, c = Path(self.data) / "k.pem", Path(self.data) / "c.pem"
        k.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        c.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        with override_settings(POLICY_KEY=str(k), POLICY_CERT=str(c)):
            other = sign(text)
            self.assertNotEqual(first, other)
            self.assertEqual(other, sign(text))
        self.assertEqual(first, sign(text), "back to the generated key: its signature is still the one kept")

    def test_two_requests_signing_at_once_end_with_one_signature(self):
        text = effective(self.alice)
        stored = sign(text)
        with mock.patch.object(PolicySignature.objects, "filter", return_value=mock.Mock(first=lambda: None)):
            with mock.patch.object(PolicySignature.objects, "get_or_create",
                                   return_value=(PolicySignature.objects.get(), False)):
                self.assertEqual(stored, sign(text))

    def test_old_signatures_are_dropped(self):
        with mock.patch.object(PolicySignature, "KEEP", 3):
            for n in range(6):
                Policy.objects.filter(org=self.org).update(data={"features": {"travel": bool(n % 2)}, "ui": {"language": f"x{n}"}}, revision=n)
                sign(effective(self.alice))
        self.assertLessEqual(PolicySignature.objects.count(), 4)
        self.assertTrue(sign(effective(self.alice)), "a dropped one is only signed again")
