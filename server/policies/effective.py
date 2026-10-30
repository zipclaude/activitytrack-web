# SPDX-License-Identifier: GPL-3.0-or-later
"""The policy a phone gets (docs/SYNC.md §8): organisation, profile, user, merged key by key."""

import base64
import hashlib
import json

from django.conf import settings

from .models import Policy

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


def sign(text):
    """The detached signature the app checks (PolicySignature), when a certificate is configured."""
    if not settings.POLICY_KEY or not settings.POLICY_CERT:
        return None
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, padding

    key = serialization.load_pem_private_key(open(settings.POLICY_KEY, "rb").read(), password=None)
    data = text.encode("utf-8")
    if isinstance(key, ec.EllipticCurvePrivateKey):
        signature, algorithm = key.sign(data, ec.ECDSA(hashes.SHA256())), "SHA256withECDSA"
    else:
        signature, algorithm = key.sign(data, padding.PKCS1v15(), hashes.SHA256()), "SHA256withRSA"
    chain = []
    for path in [settings.POLICY_CERT] + ([settings.POLICY_CHAIN] if settings.POLICY_CHAIN else []):
        for cert in x509.load_pem_x509_certificates(open(path, "rb").read()):
            chain.append(cert.public_bytes(serialization.Encoding.PEM).decode())
    return json.dumps({
        "format": "activitytrack-policy-signature",
        "algorithm": algorithm,
        "chain": chain,
        "signature": base64.b64encode(signature).decode(),
    }, indent=2) + "\n"


def fingerprint(text, signature):
    return hashlib.sha256((text + "\0" + (signature or "")).encode()).hexdigest()
