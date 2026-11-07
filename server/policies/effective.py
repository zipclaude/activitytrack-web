# SPDX-License-Identifier: GPL-3.0-or-later
"""The policy a phone gets (docs/SYNC.md §8): organisation, profile, user, merged key by key."""

import base64
import hashlib
import json
import logging

from django.conf import settings

from .models import Policy

log = logging.getLogger("updates")
# the app that trusts the server's own certificate (the `k` of its pairing) as a policy signer: older apps want
# a chain to a CA, which the generated, self-signed certificate does not have
GENERATED_KEY_FROM = (0, 9, 9)

# sections whose keys merge one by one; the others are replaced whole
MAPS = ("features", "settings", "behaviours", "integrations")


def merge(base, over):
    out = dict(base)
    for key, value in over.items():
        if key in MAPS and isinstance(value, dict):
            out[key] = {**out.get(key, {}), **value}
        elif key == "fields" and isinstance(value, dict):
            fields = dict(out.get("fields", {}))
            for name, attrs in value.items():
                fields[name] = {**fields.get(name, {}), **attrs}
            out["fields"] = fields
        else:
            out[key] = value
    return out


def layers(user):
    q = Policy.objects.filter(org=user.org)
    return [p for p in (q.filter(profile="", user__isnull=True).first(), q.filter(profile=user.profile, user__isnull=True).first(), q.filter(user=user).first()) if p]


def merged(user):
    """The merged policy body of the user's scopes ({} without any)."""
    out = {}
    for p in layers(user):
        out = merge(out, p.data)
    return out


def fixed_setting(user, key):
    """The value of a setting the policy locks or hides on the user's phones, else None."""
    s = merged(user).get("settings", {}).get(key)
    if isinstance(s, dict) and (s.get("locked") or s.get("hidden")):
        return s.get("value")
    return None


def effective(user):
    """The policy text for the user's phones, or None when no scope has one."""
    found = layers(user)
    if not found:
        return None
    body = {}
    for p in found:
        body = merge(body, p.data)
    version = ".".join(str(p.revision) for p in found)
    policy = {"format": "activitytrack-policy", "schema": 1, "id": user.org.name, "version": version, **body}
    return json.dumps(policy, ensure_ascii=False, indent=2) + "\n"


def configured():
    """True when `[policy] key` and `cert` are set: those sign the policies, for every app."""
    return bool(settings.POLICY_KEY and settings.POLICY_CERT)


def understands_generated_key(app_version):
    """Whether an app of [app_version] accepts a policy signed with the server's generated certificate.
    An app that names no version is older than the rule that makes it name one (0.9.x before 0.9.9)."""
    from sync.compat import version_key

    return version_key(app_version)[:3] >= GENERATED_KEY_FROM


def sign(text):
    """The detached signature the app checks (PolicySignature): `[policy] key`/`cert`, else the key the server made
    for itself (updates/keys.py; its CN is pinned by the phones with the first signed policy). None if the generated
    key cannot be had: the policy then goes unsigned, as it did before there was one."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, padding

    from updates import keys

    try:
        key, certs = keys.load()
    except Exception:
        if configured():
            raise
        log.exception("policies: the server's signing key cannot be had: the policy goes unsigned")
        return None
    data = text.encode("utf-8")
    if isinstance(key, ec.EllipticCurvePrivateKey):
        signature, algorithm = key.sign(data, ec.ECDSA(hashes.SHA256())), "SHA256withECDSA"
    else:
        signature, algorithm = key.sign(data, padding.PKCS1v15(), hashes.SHA256()), "SHA256withRSA"
    chain = [x509.load_der_x509_certificate(der).public_bytes(serialization.Encoding.PEM).decode() for der in certs]
    return json.dumps({
        "format": "activitytrack-policy-signature",
        "algorithm": algorithm,
        "chain": chain,
        "signature": base64.b64encode(signature).decode(),
    }, indent=2) + "\n"


def sign_for(text, app_version):
    """The signature for a device of [app_version]: the configured key signs for every app; the generated one only
    for apps that know it, the others keep getting the policy unsigned (what they got before)."""
    if configured() or understands_generated_key(app_version):
        return sign(text)
    return None


def fingerprint(text, signature):
    return hashlib.sha256((text + "\0" + (signature or "")).encode()).hexdigest()
