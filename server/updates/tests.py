# SPDX-License-Identifier: GPL-3.0-or-later
import datetime
import hashlib
import io
import json
import shutil
import stat
import tempfile
from pathlib import Path
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import Device, Organization, PairingCode, Profile, User

from . import fetch, jws, keys, relay
from .models import AcceptedSigner, MirrorFile, UpdateSettings
from .versions import compare

MANIFEST_URL = "https://example.org/update-manifest.json"
PACKAGES = {
    "https://example.org/web-0.9.9.deb": b"web 0.9.9",
    "https://example.org/web-0.9.8.deb": b"web 0.9.8",
    "https://example.org/web-0.9.7.deb": b"web 0.9.7",
    "https://example.org/web-1.0.0-beta1.deb": b"web beta",
}


def make_signer(kind="rsa"):
    """(key, certificate in DER, fingerprint) of a throwaway signer."""
    key = rsa.generate_private_key(65537, 2048) if kind == "rsa" else ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test signer")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=30)).sign(key, hashes.SHA256()))
    der = cert.public_bytes(serialization.Encoding.DER)
    return key, der, jws.fingerprint(der), cert


def sha(data):
    return hashlib.sha256(data).hexdigest()


def release(version, channel="prod", url=None, data=None):
    url = url or f"https://example.org/web-{version}.deb"
    data = data if data is not None else PACKAGES.get(url, b"x")
    return {"version": version, "channel": channel, "published": "2026-11-05", "notes_url": f"https://example.org/{version}",
            "packages": [{"component": "web-deb", "url": url, "sha256": sha(data), "size": len(data)}]}


def payload(releases=None, issued="2026-11-07T10:00:00Z"):
    return {"issued": issued, "releases": releases if releases is not None else [release("0.9.9"), release("0.9.8")]}


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Net:
    """The Internet of the tests: address → bytes."""

    def __init__(self, files):
        self.files = dict(files)
        self.asked = []

    def __call__(self, req, timeout):
        self.asked.append(req.full_url)
        if req.full_url not in self.files:
            raise OSError("unreachable")
        return Reply(self.files[req.full_url])


class UpdatesCase(TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.app_key, self.app_der, self.app_fp, _ = make_signer("rsa")
        for o in (override_settings(DATA_DIR=Path(self.dir), FILES_DIR=Path(self.dir) / "files", POLICY_KEY="", POLICY_CERT="",
                                    POLICY_CHAIN="", UPDATES_APP_FINGERPRINT=self.app_fp, UPDATES_BACKGROUND=False),):
            o.enable()
            self.addCleanup(o.disable)
        self.org = Organization.objects.create(name="Infra")
        self.admin = User.objects.create_user("root", password="x-x-x-x-x-x", org=self.org, profile=Profile.ADMIN)
        self.alice = User.objects.create_user("alice", password="x-x-x-x-x-x", org=self.org)
        self.cfg = UpdateSettings.for_org(self.org)
        self.cfg.manifest_url = MANIFEST_URL
        self.cfg.save()

    def signed(self, body=None, key=None, der=None, alg="RS256"):
        return jws.sign(body or payload(), key or self.app_key, [der or self.app_der], alg)

    def serve(self, manifest, packages=PACKAGES):
        net = Net({MANIFEST_URL: manifest.encode(), **packages})
        p = mock.patch.object(fetch, "_open", net)
        p.start()
        self.addCleanup(p.stop)
        return net

    def token(self, user=None):
        _, code = PairingCode.issue(user or self.alice)
        r = self.client.post("/api/v1/pair", json.dumps({"code": code, "deviceName": "Telefono"}), content_type="application/json")
        return r.json()["token"]

    def head(self, token):
        r = self.client.get("/api/v1/sync/head", HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def refresh(self):
        self.cfg = UpdateSettings.objects.get(pk=self.cfg.pk)
        return self.cfg


class KeyTests(UpdatesCase):
    def test_a_key_is_made_on_first_use_and_kept(self):
        fp = keys.fingerprint()
        self.assertRegex(fp, r"^[0-9a-f]{64}$")
        self.assertEqual(fp, keys.fingerprint())
        key_file = Path(self.dir) / "signing" / "server-key.pem"
        self.assertEqual(0o600, stat.S_IMODE(key_file.stat().st_mode))
        cert = x509.load_pem_x509_certificate((Path(self.dir) / "signing" / "server-cert.pem").read_bytes())
        self.assertTrue(cert.subject.rfc4514_string().startswith("CN=ActivityTrack Web "))
        self.assertGreater(cert.not_valid_after_utc - cert.not_valid_before_utc, datetime.timedelta(days=3650))
        self.assertEqual("ES256", keys.usable())
        keys._cache.clear()
        self.assertEqual(fp, keys.fingerprint(), "read from the files again")

    def test_policy_key_and_cert_are_used_when_set(self):
        key, der, fp, cert = make_signer("rsa")
        k, c = Path(self.dir) / "k.pem", Path(self.dir) / "c.pem"
        k.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        c.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        with override_settings(POLICY_KEY=str(k), POLICY_CERT=str(c)):
            self.assertEqual(fp, keys.fingerprint())
            self.assertEqual("RS256", keys.usable())
        self.assertFalse((Path(self.dir) / "signing").exists())

    def test_pairing_link_carries_the_fingerprint(self):
        self.client.force_login(self.alice)
        page = self.client.post("/dispositivi/associa/").content.decode()
        self.assertIn(f"k={keys.fingerprint()}", page)


class AcceptTests(UpdatesCase):
    def accept(self, text):
        return fetch.accept(self.cfg, text)

    def test_the_apps_key_is_accepted(self):
        self.assertEqual("0.9.9", self.accept(self.signed())["releases"][0]["version"])
        self.assertEqual(("0.9.9", "2026-11-07T10:00:00Z"), (self.cfg.top_version, self.cfg.issued))

    def test_a_stranger_is_refused_until_its_fingerprint_is_accepted(self):
        key, der, fp, _ = make_signer("ec")
        text = self.signed(key=key, der=der, alg="ES256")
        with self.assertRaises(jws.ManifestError):
            self.accept(text)
        AcceptedSigner.objects.create(org=self.org, fingerprint=jws.normalize(jws.display(fp).upper()))
        self.accept(text)

    def test_signers_of_another_organisation_do_not_count(self):
        key, der, fp, _ = make_signer("ec")
        other = Organization.objects.create(name="Other")
        AcceptedSigner.objects.create(org=other, fingerprint=fp)
        with self.assertRaises(jws.ManifestError):
            self.accept(self.signed(key=key, der=der, alg="ES256"))

    def test_bad_signature_refused(self):
        text = json.loads(self.signed())
        doc = json.loads(jws.unb64u(text["payload"]))
        doc["releases"][0]["version"] = "9.9.9"
        text["payload"] = jws.b64u(json.dumps(doc).encode())
        with self.assertRaises(jws.ManifestError):
            self.accept(json.dumps(text))

    def test_other_algorithms_refused(self):
        text = json.loads(self.signed())
        header = json.loads(jws.unb64u(text["signatures"][0]["protected"]))
        for alg in ("none", "HS256"):
            header["alg"] = alg
            text["signatures"][0]["protected"] = jws.b64u(json.dumps(header).encode())
            with self.assertRaises(jws.ManifestError, msg=alg):
                self.accept(json.dumps(text))

    def test_package_addresses_must_be_https(self):
        with self.assertRaises(jws.ManifestError):
            self.accept(self.signed(payload([release("0.9.9", url="http://example.org/web.deb")])))

    def test_package_without_sha256_refused(self):
        r = release("0.9.9")
        r["packages"][0]["sha256"] = ""
        with self.assertRaises(jws.ManifestError):
            self.accept(self.signed(payload([r])))
        del r["packages"][0]["sha256"]
        with self.assertRaises(jws.ManifestError):
            self.accept(self.signed(payload([r])))

    def test_downgrade_and_replay_refused(self):
        self.accept(self.signed(payload([release("0.9.9")], issued="2026-11-07T10:00:00Z")))
        with self.assertRaises(jws.ManifestError):
            self.accept(self.signed(payload([release("0.9.8")], issued="2026-11-08T10:00:00Z")))
        with self.assertRaises(jws.ManifestError):
            self.accept(self.signed(payload([release("0.9.9")], issued="2026-11-06T10:00:00Z")))
        self.accept(self.signed(payload([release("0.9.9"), release("1.0.0-beta1", "beta")], issued="2026-11-08T10:00:00Z")))
        self.assertEqual("1.0.0-beta1", self.cfg.top_version)

    def test_version_order(self):
        self.assertEqual(-1, compare("1.0.0-beta1", "1.0.0"))
        self.assertEqual(1, compare("0.9.10", "0.9.9"))


class RunTests(UpdatesCase):
    def test_run_keeps_the_manifest_and_signs_it_for_the_devices(self):
        self.serve(self.signed())
        self.assertEqual("", fetch.run(self.cfg))
        self.refresh()
        self.assertEqual(("0.9.9", ""), (self.cfg.top_version, self.cfg.last_error))
        self.assertIsNotNone(self.cfg.last_ok)
        own = jws.verify(self.cfg.relayed, [keys.fingerprint()])
        self.assertEqual(["0.9.9", "0.9.8"], [r["version"] for r in own["releases"]])
        with self.assertRaises(jws.ManifestError):
            jws.verify(self.cfg.relayed, [self.app_fp])

    def test_a_refused_manifest_leaves_the_previous_one(self):
        self.serve(self.signed())
        fetch.run(self.cfg)
        key, der, _fp, _ = make_signer("ec")
        self.serve(self.signed(key=key, der=der, alg="ES256"))
        self.assertIn("no valid signature", fetch.run(self.refresh()))
        self.refresh()
        self.assertEqual("0.9.9", self.cfg.top_version)
        self.assertIn("no valid signature", self.cfg.last_error)
        self.assertTrue(self.cfg.relayed)

    def test_unreachable_address_is_an_error_not_a_crash(self):
        self.serve(self.signed())
        self.cfg.manifest_url = "https://example.org/other.json"
        self.assertEqual("unreachable", fetch.run(self.cfg))

    def test_the_manifest_address_must_be_https(self):
        self.cfg.manifest_url = "http://example.org/update-manifest.json"
        self.assertIn("https", fetch.run(self.cfg))

    def test_due_and_claimed_once(self):
        self.assertTrue(fetch.due(self.cfg))
        self.serve(self.signed())
        self.assertTrue(fetch.poll_if_due(self.cfg))
        self.assertEqual("0.9.9", self.refresh().top_version)
        self.assertFalse(fetch.due(self.cfg))
        self.assertFalse(fetch.poll_if_due(self.cfg))
        self.cfg.last_checked = timezone.now() - datetime.timedelta(hours=13)
        self.assertTrue(fetch.due(self.cfg))
        self.cfg.enabled = False
        self.assertFalse(fetch.due(self.cfg))
        self.cfg.enabled, self.cfg.poll_hours = True, 0
        self.assertFalse(fetch.due(self.cfg))

    def test_after_an_error_it_tries_again_within_the_hour(self):
        self.cfg.last_checked = timezone.now() - datetime.timedelta(hours=2)
        self.cfg.last_error = "unreachable"
        self.assertTrue(fetch.due(self.cfg))
        self.cfg.last_checked = timezone.now() - datetime.timedelta(minutes=10)
        self.assertFalse(fetch.due(self.cfg))

    def test_a_thread_is_started_when_background_is_on(self):
        with override_settings(UPDATES_BACKGROUND=True), mock.patch.object(fetch.threading, "Thread") as thread:
            self.assertTrue(fetch.poll_if_due(self.cfg))
        thread.return_value.start.assert_called_once()

    def test_command(self):
        self.serve(self.signed())
        out = io.StringIO()
        call_command("fetch_updates", stdout=out)
        self.assertIn("ok, top version 0.9.9", out.getvalue())
        out = io.StringIO()
        call_command("fetch_updates", stdout=out)
        self.assertEqual("", out.getvalue(), "not due again")
        call_command("fetch_updates", "--force", stdout=out)
        self.assertIn("ok", out.getvalue())


class MirrorTests(UpdatesCase):
    def run_with(self, releases, packages=PACKAGES, issued="2026-11-07T10:00:00Z"):
        self.serve(self.signed(payload(releases, issued)), packages)
        fetch.run(self.refresh())
        return self.refresh()

    def relayed(self):
        return jws.verify(self.refresh().relayed, [keys.fingerprint()])

    def test_packages_are_kept_checked_and_served_by_the_server(self):
        self.run_with([release("0.9.9")])
        row = MirrorFile.objects.get()
        self.assertEqual((row.version, row.sha256), ("0.9.9", sha(b"web 0.9.9")))
        self.assertEqual(b"web 0.9.9", fetch.mirror_path(row.sha256).read_bytes())
        package = self.relayed()["releases"][0]["packages"][0]
        self.assertEqual(f"/api/v1/update/files/{sha(b'web 0.9.9')}", package["url"])
        self.assertEqual(sha(b"web 0.9.9"), package["sha256"])

    def test_a_package_that_does_not_match_is_not_kept_nor_offered(self):
        bad = dict(PACKAGES, **{"https://example.org/web-0.9.9.deb": b"tampered"})
        self.run_with([release("0.9.9"), release("0.9.8")], bad)
        self.assertEqual(["0.9.8"], [m.version for m in MirrorFile.objects.all()])
        self.assertEqual(["0.9.8"], [r["version"] for r in self.relayed()["releases"]])
        self.assertEqual([], [p.name for p in fetch.mirror_dir().iterdir() if p.name.endswith(".part")])
        # the next check finds it fixed
        self.serve(self.signed(payload([release("0.9.9"), release("0.9.8")], "2026-11-07T11:00:00Z")))
        fetch.run(self.refresh())
        self.assertEqual(["0.9.9", "0.9.8"], [r["version"] for r in self.relayed()["releases"]])

    def test_unreachable_package_left_out(self):
        self.run_with([release("0.9.9")], packages={})
        self.assertFalse(MirrorFile.objects.exists())
        self.assertEqual([], self.relayed()["releases"])

    def test_mirror_off_leaves_the_addresses(self):
        self.cfg.mirror = False
        self.cfg.save()
        self.run_with([release("0.9.9")])
        self.assertFalse(MirrorFile.objects.exists())
        self.assertEqual("https://example.org/web-0.9.9.deb", self.relayed()["releases"][0]["packages"][0]["url"])

    def test_switching_the_mirror_changes_what_phones_get(self):
        self.run_with([release("0.9.9")])
        self.client.force_login(self.admin)
        self.client.post("/aggiornamenti/salva/", {"manifest_url": MANIFEST_URL, "poll_hours": "12", "channels": "prod"})
        self.assertEqual("https://example.org/web-0.9.9.deb", self.relayed()["releases"][0]["packages"][0]["url"])

    def test_only_followed_channels_are_mirrored(self):
        self.run_with([release("0.9.9"), release("1.0.0-beta1", "beta")])
        self.assertEqual(["0.9.9"], [m.version for m in MirrorFile.objects.all()])
        urls = {r["version"]: r["packages"][0]["url"] for r in self.relayed()["releases"]}
        self.assertTrue(urls["0.9.9"].startswith("/api/v1/update/files/"))
        self.assertEqual("https://example.org/web-1.0.0-beta1.deb", urls["1.0.0-beta1"])

    def test_the_last_two_releases_are_always_kept(self):
        self.run_with([release("0.9.7")])
        self.assertEqual(["0.9.7"], [m.version for m in MirrorFile.objects.all()])
        self.run_with([release("0.9.8"), release("0.9.7")], issued="2026-11-07T11:00:00Z")
        self.assertEqual({"0.9.7", "0.9.8"}, {m.version for m in MirrorFile.objects.all()})
        # a newer one pushes the oldest out, and only then
        self.run_with([release("0.9.9"), release("0.9.8")], issued="2026-11-07T12:00:00Z")
        self.assertEqual({"0.9.8", "0.9.9"}, {m.version for m in MirrorFile.objects.all()})
        self.assertFalse(fetch.mirror_path(sha(b"web 0.9.7")).exists())
        self.assertTrue(fetch.mirror_path(sha(b"web 0.9.8")).exists())

    def test_a_failed_newest_does_not_cost_an_old_one(self):
        self.run_with([release("0.9.8"), release("0.9.7")])
        self.run_with([release("0.9.9"), release("0.9.8"), release("0.9.7")], packages={
            "https://example.org/web-0.9.8.deb": PACKAGES["https://example.org/web-0.9.8.deb"]}, issued="2026-11-07T11:00:00Z")
        self.assertEqual({"0.9.7", "0.9.8"}, {m.version for m in MirrorFile.objects.all()})

    def test_size_cap(self):
        with override_settings(UPDATES_MAX_MB=0):
            self.run_with([release("0.9.9")])
        self.assertFalse(MirrorFile.objects.exists())

    def test_same_file_for_two_versions_is_deleted_only_with_the_last(self):
        same = {"https://example.org/web-0.9.7.deb": b"same", "https://example.org/web-0.9.8.deb": b"same"}
        self.run_with([release("0.9.7", data=b"same"), release("0.9.8", data=b"same")], same)
        self.run_with([release("0.9.9"), release("0.9.8", data=b"same")], dict(PACKAGES, **same), issued="2026-11-07T11:00:00Z")
        self.assertTrue(fetch.mirror_path(sha(b"same")).exists())


class DeviceTests(UpdatesCase):
    def test_head_without_a_manifest_says_null(self):
        self.assertIsNone(self.head(self.token())["update"])

    def test_head_carries_the_servers_signed_manifest(self):
        token = self.token()
        self.serve(self.signed())
        fetch.run(self.cfg)
        text = self.head(token)["update"]
        self.assertEqual("0.9.9", jws.verify(text, [keys.fingerprint()])["releases"][0]["version"])
        # signed again only when something changed
        self.assertEqual(text, self.head(token)["update"])

    def test_the_first_head_after_a_stale_manifest_starts_the_check(self):
        token = self.token()
        self.serve(self.signed())
        self.assertEqual("0.9.9", jws.verify(self.head(token)["update"], [keys.fingerprint()])["releases"][0]["version"])

    def test_another_key_signs_again(self):
        token = self.token()
        self.serve(self.signed())
        fetch.run(self.cfg)
        first = self.head(token)["update"]
        key, der, fp, cert = make_signer("rsa")
        k, c = Path(self.dir) / "k.pem", Path(self.dir) / "c.pem"
        k.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        c.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        with override_settings(POLICY_KEY=str(k), POLICY_CERT=str(c)):
            second = self.head(token)["update"]
            self.assertNotEqual(first, second)
            self.assertTrue(jws.verify(second, [fp]))

    def test_mirrored_package_download(self):
        token = self.token()
        self.serve(self.signed(payload([release("0.9.9")])))
        fetch.run(self.cfg)
        url = json.loads(jws.unb64u(json.loads(self.head(token)["update"])["payload"]))["releases"][0]["packages"][0]["url"]
        r = self.client.get(url, HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual((200, b"web 0.9.9"), (r.status_code, b"".join(r.streaming_content)))

    def test_download_needs_a_device_and_a_known_file(self):
        token = self.token()
        self.serve(self.signed(payload([release("0.9.9")])))
        fetch.run(self.cfg)
        path = relay.path(sha(b"web 0.9.9"))
        self.assertEqual(401, self.client.get(path).status_code)
        self.assertEqual(404, self.client.get(relay.path("0" * 64), HTTP_AUTHORIZATION=f"Bearer {token}").status_code)
        other = Organization.objects.create(name="Other")
        stranger = User.objects.create_user("eve", password="x-x-x-x-x-x", org=other)
        self.assertEqual(404, self.client.get(path, HTTP_AUTHORIZATION=f"Bearer {self.token(stranger)}").status_code)

    def test_older_clients_still_get_their_head(self):
        head = self.head(self.token())
        for key in ("rev", "serverTime", "filesMaxMb", "pluginHolder", "mapsRev", "server", "schema"):
            self.assertIn(key, head)
        self.assertEqual(1, Device.objects.count())


class PageTests(UpdatesCase):
    def test_only_an_admin(self):
        self.client.force_login(self.alice)
        for path in ("/aggiornamenti/",):
            self.assertEqual(403, self.client.get(path).status_code)
        for path in ("/aggiornamenti/salva/", "/aggiornamenti/controlla/", "/aggiornamenti/chiavi/aggiungi/"):
            self.assertEqual(403, self.client.post(path).status_code, path)

    def test_page_shows_both_fingerprints_and_the_newer_server(self):
        self.serve(self.signed(payload([release("9.9.9"), release("0.9.8")])))
        fetch.run(self.cfg)
        self.client.force_login(self.admin)
        page = self.client.get("/aggiornamenti/").content.decode()
        self.assertIn(jws.display(keys.fingerprint()), page)
        self.assertIn(jws.display(self.app_fp), page)
        self.assertIn("9.9.9", page)

    def test_save_settings(self):
        self.client.force_login(self.admin)
        r = self.client.post("/aggiornamenti/salva/", {"manifest_url": "https://example.net/m.json", "poll_hours": "6",
                                                       "channels": ["prod", "beta", "nonsense"], "enabled": "on", "mirror": "on"})
        self.assertEqual(302, r.status_code)
        cfg = self.refresh()
        self.assertEqual(("https://example.net/m.json", 6, "prod,beta", True, True),
                         (cfg.manifest_url, cfg.poll_hours, cfg.channels, cfg.enabled, cfg.mirror))

    def test_save_refuses_a_plain_http_address_and_a_bad_interval(self):
        self.client.force_login(self.admin)
        self.client.post("/aggiornamenti/salva/", {"manifest_url": "http://example.net/m.json", "poll_hours": "6"})
        self.client.post("/aggiornamenti/salva/", {"manifest_url": "https://example.net/m.json", "poll_hours": "x"})
        self.assertEqual(MANIFEST_URL, self.refresh().manifest_url)

    def test_signers_added_by_fingerprint_and_removed(self):
        self.client.force_login(self.admin)
        fp = "AB:" * 31 + "CD"
        with self.assertLogs("updates", "INFO") as log:
            self.client.post("/aggiornamenti/chiavi/aggiungi/", {"fingerprint": fp, "label": "Build server"})
            self.client.post("/aggiornamenti/chiavi/aggiungi/", {"fingerprint": fp})
            self.client.post("/aggiornamenti/chiavi/aggiungi/", {"fingerprint": "12:34"})
            signer = AcceptedSigner.objects.get()
            self.assertEqual(("ab" * 31 + "cd", "Build server", self.admin), (signer.fingerprint, signer.label, signer.added_by))
            self.client.post(f"/aggiornamenti/chiavi/{signer.pk}/togli/")
        self.assertFalse(AcceptedSigner.objects.exists())
        self.assertEqual(2, len(log.output), "the addition and the removal are logged with who did it")
        self.assertIn("root", log.output[0])

    def test_one_cannot_remove_another_organisations_signer(self):
        other = Organization.objects.create(name="Other")
        signer = AcceptedSigner.objects.create(org=other, fingerprint="ab" * 32)
        self.client.force_login(self.admin)
        self.assertEqual(404, self.client.post(f"/aggiornamenti/chiavi/{signer.pk}/togli/").status_code)

    def test_check_now(self):
        self.serve(self.signed())
        self.client.force_login(self.admin)
        self.client.post("/aggiornamenti/controlla/")
        self.assertEqual("0.9.9", self.refresh().top_version)
