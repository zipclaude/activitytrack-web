# SPDX-License-Identifier: GPL-3.0-or-later
import datetime
import hashlib
import http.client
import importlib
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

from . import distro, fetch, jws, keys, relay
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


def release(version, channel="prod", url=None, data=None, component="web-deb13"):
    url = url or f"https://example.org/web-{version}.deb"
    data = data if data is not None else PACKAGES.get(url, b"x")
    return {"version": version, "channel": channel, "published": "2026-11-05", "notes_url": f"https://example.org/{version}",
            "packages": [{"component": component, "url": url, "sha256": sha(data), "size": len(data)}]}


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
        # the _utc readers came with cryptography 42 (Ubuntu 24.04 has 41): the other two are the same moments
        after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
        before = getattr(cert, "not_valid_before_utc", None) or cert.not_valid_before
        self.assertGreater(after - before, datetime.timedelta(days=3650))
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


class KeepTests(UpdatesCase):
    """The newest two releases of each followed channel are mirrored and kept, and nothing else is downloaded."""

    def poll(self, releases, issued, channels=None):
        if channels:
            self.cfg.channels = channels
            self.cfg.save()
        self.net = self.serve(self.signed(payload(releases, issued)), {
            **PACKAGES, "https://example.org/web-1.0.0-beta1.deb": b"beta1", "https://example.org/web-1.0.0-beta2.deb": b"beta2",
            "https://example.org/web-1.0.0-beta3.deb": b"beta3"})
        fetch.run(self.refresh())
        return self.refresh()

    def kept(self):
        return sorted(m.version for m in MirrorFile.objects.all())

    def downloads(self):
        return [u for u in self.net.asked if u != MANIFEST_URL]

    def relayed(self):
        return [r["version"] for r in jws.verify(self.refresh().relayed, [keys.fingerprint()])["releases"]]

    def beta(self, n):
        return release(f"1.0.0-beta{n}", "beta", data=f"beta{n}".encode())

    def test_three_prod_releases_download_two_and_nothing_again(self):
        three = [release("0.9.9"), release("0.9.8"), release("0.9.7")]
        self.poll(three, "2026-11-07T10:00:00Z")
        self.assertEqual(["0.9.8", "0.9.9"], self.kept())
        self.assertEqual(2, len(self.downloads()))
        self.assertNotIn("https://example.org/web-0.9.7.deb", self.downloads())
        self.assertEqual(["0.9.9", "0.9.8"], self.relayed())
        # the second poll, and the third: the manifest only
        for n in (11, 12):
            self.poll(three, f"2026-11-07T{n}:00:00Z")
            self.assertEqual([], self.downloads(), n)
        self.assertEqual(["0.9.8", "0.9.9"], self.kept())

    def test_betas_newer_than_prod_do_not_push_prod_out(self):
        releases = [release("0.9.9"), self.beta(3), self.beta(2), self.beta(1)]
        self.poll(releases, "2026-11-07T10:00:00Z", channels="prod,beta")
        self.assertEqual(["0.9.9", "1.0.0-beta2", "1.0.0-beta3"], self.kept())
        self.assertEqual({"0.9.9", "1.0.0-beta3", "1.0.0-beta2"}, set(self.relayed()))
        self.assertEqual(3, len(self.downloads()))
        self.poll(releases, "2026-11-07T11:00:00Z")
        self.assertEqual([], self.downloads(), "no download again")
        self.assertEqual({"0.9.9", "1.0.0-beta3", "1.0.0-beta2"}, set(self.relayed()))

    def test_two_prod_and_two_beta_releases_are_all_kept(self):
        self.poll([release("0.9.9"), release("0.9.8"), self.beta(2), self.beta(1)], "2026-11-07T10:00:00Z", channels="prod,beta")
        self.assertEqual(["0.9.8", "0.9.9", "1.0.0-beta1", "1.0.0-beta2"], self.kept())
        self.assertEqual(4, len(self.relayed()))

    def test_a_new_release_pushes_the_oldest_of_its_channel_out_only(self):
        self.poll([release("0.9.8"), release("0.9.7"), self.beta(1)], "2026-11-07T10:00:00Z", channels="prod,beta")
        self.poll([release("0.9.9"), release("0.9.8"), release("0.9.7"), self.beta(1)], "2026-11-07T11:00:00Z")
        self.assertEqual(["0.9.8", "0.9.9", "1.0.0-beta1"], self.kept())
        self.assertEqual(["https://example.org/web-0.9.9.deb"], self.downloads())
        self.assertFalse(fetch.mirror_path(sha(b"web 0.9.7")).exists())
        self.assertTrue(fetch.mirror_path(sha(b"web 0.9.8")).exists())

    def test_a_channel_no_longer_followed_loses_its_files(self):
        self.poll([release("0.9.9"), self.beta(1)], "2026-11-07T10:00:00Z", channels="prod,beta")
        self.assertEqual(["0.9.9", "1.0.0-beta1"], self.kept())
        self.poll([release("0.9.9"), self.beta(1)], "2026-11-07T11:00:00Z", channels="prod")
        self.assertEqual(["0.9.9"], self.kept())
        self.assertFalse(fetch.mirror_path(sha(b"beta1")).exists())

    def test_wanted(self):
        self.cfg.payload = payload([release("0.9.9"), release("0.9.8"), release("0.9.7"), self.beta(1)])
        self.assertEqual({"prod": {"0.9.9", "0.9.8"}}, fetch.wanted(self.cfg))
        self.cfg.channels = "prod,beta,dev"
        self.assertEqual({"prod": {"0.9.9", "0.9.8"}, "beta": {"1.0.0-beta1"}}, fetch.wanted(self.cfg))

    def test_migration_gives_the_files_their_channel(self):
        self.poll([release("0.9.9"), self.beta(1)], "2026-11-07T10:00:00Z", channels="prod,beta")
        MirrorFile.objects.update(channel="prod")
        from django.apps import apps
        importlib.import_module("updates.migrations.0003_mirror_channel").fill_channels(apps, None)
        self.assertEqual({"0.9.9": "prod", "1.0.0-beta1": "beta"}, {m.version: m.channel for m in MirrorFile.objects.all()})


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


class DistroTests(TestCase):
    def test_components_of_the_supported_distributions(self):
        for info, want in (({"ID": "debian", "VERSION_ID": "13"}, "web-deb13"),
                           ({"ID": "debian", "VERSION_ID": "14"}, "web-deb14"),
                           ({"ID": "debian", "VERSION_CODENAME": "forky"}, "web-deb14"),
                           ({"ID": "ubuntu", "VERSION_ID": "24.04"}, "web-ubuntu24.04"),
                           ({"ID": "ubuntu", "VERSION_ID": "26.04"}, "web-ubuntu26.04"),
                           ({"ID": "ubuntu", "VERSION_ID": "22.04"}, None),
                           ({"ID": "debian", "VERSION_ID": "12"}, None),
                           ({"ID": "arch"}, None), ({}, None)):
            with mock.patch.object(distro, "os_release", return_value=info):
                self.assertEqual(want, distro.component(), info)

    def test_os_release_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "os-release"
            f.write_text('# a comment\nPRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nID=debian\nVERSION_ID="13"\n')
            self.assertEqual("web-deb13", distro.component(path=f))
            self.assertEqual("library-deb13", distro.component("library", f))
            self.assertIsNone(distro.component(path=Path(d) / "missing"))


class PageTests(UpdatesCase):
    def test_only_an_admin(self):
        self.client.force_login(self.alice)
        for path in ("/aggiornamenti/",):
            self.assertEqual(403, self.client.get(path).status_code)
        for path in ("/aggiornamenti/salva/", "/aggiornamenti/controlla/", "/aggiornamenti/chiavi/aggiungi/"):
            self.assertEqual(403, self.client.post(path).status_code, path)

    def page_with(self, releases, os_info):
        self.serve(self.signed(payload(releases)))
        fetch.run(self.cfg)
        self.client.force_login(self.admin)
        with mock.patch.object(distro, "os_release", return_value=os_info):
            return self.client.get("/aggiornamenti/").content.decode()

    def test_page_shows_both_fingerprints(self):
        page = self.page_with([release("0.9.8")], {"ID": "debian", "VERSION_ID": "13"})
        self.assertIn(jws.display(keys.fingerprint()), page)
        self.assertIn(jws.display(self.app_fp), page)

    def test_newer_server_notice_follows_the_servers_own_distribution(self):
        releases = [release("9.9.9", component="web-ubuntu24.04"), release("8.8.8", component="web-deb13"), release("0.9.8")]
        page = self.page_with(releases, {"ID": "debian", "VERSION_ID": "13"})
        self.assertIn("ActivityTrack Web 8.8.8 (", page)
        self.assertNotIn("ActivityTrack Web 9.9.9 (", page)
        page = self.page_with(releases, {"ID": "ubuntu", "VERSION_ID": "24.04"})
        self.assertIn("ActivityTrack Web 9.9.9 (", page)

    def test_newer_server_on_a_distribution_without_our_package_shows_the_version_only(self):
        releases = [release("9.9.9", component="web-ubuntu24.04"), release("0.9.8", component="desktop-windows")]
        page = self.page_with(releases, {"ID": "arch"})
        self.assertIn("ActivityTrack Web 9.9.9 (", page)
        self.assertNotIn(".deb", page)
        # a release that has no web package at all is not about this server
        self.assertNotIn("ActivityTrack Web 7.7.7 (", self.page_with([release("7.7.7", component="desktop-deb13")], {"ID": "arch"}))

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


class FailureTests(UpdatesCase):
    """What went wrong is told on the page and retried; one organisation never stops the others."""

    def test_a_truncated_reply_is_an_error_not_a_crash(self):
        for error in (http.client.IncompleteRead(b"par"), http.client.BadStatusLine("")):
            with mock.patch.object(fetch, "_open", side_effect=error):
                self.assertTrue(fetch.run(self.refresh()), error)
            self.refresh()
            self.assertTrue(self.cfg.last_error)
        self.cfg.last_checked = timezone.now() - datetime.timedelta(hours=2)
        self.assertTrue(fetch.due(self.cfg), "tried again within the hour")

    def test_a_truncated_package_is_left_out(self):
        net = Net({MANIFEST_URL: self.signed(payload([release("0.9.9")])).encode()})

        def opener(req, timeout):
            if req.full_url == MANIFEST_URL:
                return net(req, timeout)
            raise http.client.IncompleteRead(b"half")

        with mock.patch.object(fetch, "_open", opener):
            self.assertEqual("", fetch.run(self.cfg))
        self.assertFalse(MirrorFile.objects.exists())
        self.assertEqual([], jws.verify(self.refresh().relayed, [keys.fingerprint()])["releases"])

    def test_anything_else_is_recorded_too(self):
        self.serve(self.signed())
        with mock.patch.object(fetch, "accept", side_effect=TypeError("can't compare")), self.assertLogs("updates", "ERROR"):
            self.assertIn("TypeError", fetch.run(self.cfg))
        self.assertIn("TypeError", self.refresh().last_error)

    def test_a_failure_while_keeping_the_packages_is_recorded(self):
        self.serve(self.signed())
        with mock.patch.object(fetch, "mirror", side_effect=RuntimeError("disk full")), self.assertLogs("updates", "ERROR"):
            self.assertIn("disk full", fetch.run(self.cfg))
        self.assertIn("disk full", self.refresh().last_error)

    def test_command_goes_on_with_the_other_organisations(self):
        other = Organization.objects.create(name="Other")
        cfg2 = UpdateSettings.for_org(other)
        cfg2.manifest_url = MANIFEST_URL
        cfg2.save()
        self.serve(self.signed())
        real = fetch.run

        def run(cfg):
            if cfg.org == self.org:
                raise RuntimeError("boom")
            return real(cfg)

        out = io.StringIO()
        with mock.patch.object(fetch, "run", run):
            call_command("fetch_updates", stdout=out)
        self.assertIn("Infra: refused: RuntimeError: boom", out.getvalue())
        self.assertIn("Other: ok, top version 0.9.9", out.getvalue())


class TimeTests(UpdatesCase):
    def test_a_time_without_an_offset_is_utc(self):
        self.assertEqual(fetch._when("2026-11-07T10:00:00"), fetch._when("2026-11-07T10:00:00Z"))
        self.assertEqual(fetch._when("2026-11-07T11:00:00+01:00"), fetch._when("2026-11-07T10:00:00Z"))
        self.assertIsNone(fetch._when("yesterday"))

    def test_naive_and_aware_issue_times_compare(self):
        fetch.accept(self.cfg, self.signed(payload(issued="2026-11-07T10:00:00")))
        fetch.accept(self.cfg, self.signed(payload(issued="2026-11-07T11:00:00Z")))
        with self.assertRaises(jws.ManifestError):
            fetch.accept(self.cfg, self.signed(payload(issued="2026-11-07T10:30:00")))
        fetch.accept(self.cfg, self.signed(payload(issued="2026-11-07T12:00:00+00:00")))
        self.assertEqual("2026-11-07T12:00:00+00:00", self.cfg.issued)


class ChannelTopTests(UpdatesCase):
    def accept(self, releases, issued):
        return fetch.accept(self.cfg, self.signed(payload(releases, issued)))

    def test_a_withdrawn_beta_nobody_follows_blocks_nothing(self):
        self.accept([release("0.9.9"), release("1.0.0-beta1", "beta")], "2026-11-07T10:00:00Z")
        self.assertEqual({"prod": "0.9.9", "beta": "1.0.0-beta1"}, self.cfg.tops)
        self.accept([release("0.9.9")], "2026-11-07T11:00:00Z")
        self.accept([release("0.9.9"), release("0.9.5", "beta")], "2026-11-07T12:00:00Z")

    def test_a_followed_channel_does_not_go_back(self):
        self.cfg.channels = "prod,beta"
        self.accept([release("0.9.9"), release("1.0.0-beta2", "beta")], "2026-11-07T10:00:00Z")
        with self.assertRaises(jws.ManifestError) as e:
            self.accept([release("0.9.9"), release("1.0.0-beta1", "beta")], "2026-11-07T11:00:00Z")
        self.assertIn("beta", str(e.exception))
        with self.assertRaises(jws.ManifestError):
            self.accept([release("0.9.8")], "2026-11-07T11:00:00Z")
        self.accept([release("0.9.9"), release("1.0.0-beta2", "beta"), release("1.0.0-beta3", "beta")], "2026-11-07T11:00:00Z")

    def test_a_channel_that_disappears_is_not_a_downgrade(self):
        self.cfg.channels = "prod,beta"
        self.accept([release("0.9.9"), release("1.0.0-beta2", "beta")], "2026-11-07T10:00:00Z")
        self.accept([release("0.9.9")], "2026-11-07T11:00:00Z")

    def test_admin_can_reset(self):
        self.cfg.channels = "prod"
        self.cfg.save()
        self.serve(self.signed(payload([release("0.9.9")], "2026-11-07T10:00:00Z")))
        fetch.run(self.refresh())
        self.serve(self.signed(payload([release("0.9.8")], "2026-11-07T11:00:00Z")))
        self.assertIn("lower version", fetch.run(self.refresh()))
        self.client.force_login(self.alice)
        self.assertEqual(403, self.client.post("/aggiornamenti/azzera/").status_code)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get("/aggiornamenti/"), "/aggiornamenti/azzera/")
        with self.assertLogs("updates", "INFO") as log:
            self.assertEqual(302, self.client.post("/aggiornamenti/azzera/").status_code)
        self.assertIn("root", log.output[0])
        self.assertEqual(({}, "", ""), (self.refresh().tops, self.cfg.top_version, self.cfg.issued))
        self.assertEqual("", fetch.run(self.cfg))
        self.assertEqual("0.9.8", self.refresh().top_version)

    def test_migration_fills_the_tops_of_what_was_accepted(self):
        self.cfg.payload = payload([release("0.9.9"), release("0.9.8"), release("1.0.0-beta2", "beta"), release("1.0.0-beta1", "beta")])
        self.cfg.tops = {}
        self.cfg.save()
        from django.apps import apps
        importlib.import_module("updates.migrations.0002_tops").fill_tops(apps, None)
        self.assertEqual({"prod": "0.9.9", "beta": "1.0.0-beta2"}, self.refresh().tops)


class NotesLinkTests(UpdatesCase):
    def page(self):
        self.client.force_login(self.admin)
        with mock.patch.object(distro, "os_release", return_value={"ID": "debian", "VERSION_ID": "13"}):
            return self.client.get("/aggiornamenti/").content.decode()

    def test_a_notes_address_that_is_not_https_is_dropped(self):
        for bad in ("javascript:alert(1)", "http://example.org/n", "data:text/html,x", " https://example.org/n", "https://exa mple.org/"):
            r = release("9.9.9")
            r["notes_url"] = bad
            fetch.accept(self.cfg, self.signed(payload([r], "2026-11-07T1%d:00:00Z" % (len(bad) % 10))))
            self.assertEqual("", self.cfg.payload["releases"][0]["notes_url"], bad)
            self.cfg.issued, self.cfg.tops = "", {}

    def test_a_malformed_notes_address_drops_the_link_not_the_manifest(self):
        r = release("9.9.9")
        r["notes_url"] = "https://[bad"
        fetch.accept(self.cfg, self.signed(payload([r])))
        self.assertEqual("9.9.9", self.cfg.payload["releases"][0]["version"])
        self.assertEqual("", self.cfg.payload["releases"][0]["notes_url"])
        self.assertFalse(fetch._https("https://[bad"))

    def test_an_https_address_is_kept_and_linked(self):
        fetch.accept(self.cfg, self.signed(payload([release("9.9.9")])))
        self.cfg.save()
        self.assertIn('href="https://example.org/9.9.9"', self.page())

    def test_the_page_does_not_trust_what_is_stored(self):
        r = release("9.9.9")
        r["notes_url"] = "javascript:alert(1)"
        self.cfg.payload = payload([r])
        self.cfg.save()
        page = self.page()
        self.assertIn("ActivityTrack Web 9.9.9 (", page)
        self.assertNotIn("javascript:", page)


class PairingWithoutAKeyTests(UpdatesCase):
    def test_pairing_goes_on_without_k_when_the_key_cannot_be_read(self):
        self.client.force_login(self.alice)
        with mock.patch.object(keys, "fingerprint", side_effect=OSError("permission denied")), self.assertLogs("updates", "ERROR"):
            r = self.client.post("/dispositivi/associa/")
        self.assertEqual(200, r.status_code)
        page = r.content.decode()
        self.assertIn("activitytrack://pair?", page)
        self.assertNotIn("&amp;k=", page)
        self.assertEqual(1, PairingCode.objects.count())
