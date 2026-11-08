# SPDX-License-Identifier: GPL-3.0-or-later
"""
The update manifest as a JWS in JSON serialization (RFC 7515), the same format as the PC library's
`activitytrack.manifest` and the app's: ES256 and RS256 only; the signer's certificate in the
protected header (x5c); a signature counts when its leaf's SHA-256 fingerprint is a trusted one.
"""

import base64
import hashlib
import json

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils

FORMAT = "activitytrack-update-manifest"
ALGORITHMS = ("ES256", "RS256")
MAX_BYTES = 1024 * 1024


class ManifestError(Exception):
    """Not a manifest, not signed by a trusted signer, or not acceptable."""


def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64u(text):
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError) as e:
        raise ManifestError("bad base64") from e


def fingerprint(cert_der):
    return hashlib.sha256(cert_der).hexdigest()


def normalize(fp):
    """A fingerprint as an administrator pastes it ("AB:CD …"): lower case hex only."""
    return "".join(c for c in str(fp).lower() if c in "0123456789abcdef")


def display(fp):
    """"ab:cd:…" for reading aloud and comparing."""
    fp = normalize(fp)
    return ":".join(fp[i:i + 2] for i in range(0, len(fp), 2))


def _valid(cert, alg, signing_input, signature):
    key = cert.public_key()
    try:
        if alg == "ES256":
            if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1" or len(signature) != 64:
                return False
            der = utils.encode_dss_signature(int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big"))
            key.verify(der, signing_input, ec.ECDSA(hashes.SHA256()))
        else:
            if not isinstance(key, rsa.RSAPublicKey):
                return False
            key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        return False
    return True


def verify(text, trusted):
    """The payload (a dict) when a signature is valid and its leaf's fingerprint is in [trusted]."""
    pins = {normalize(f) for f in trusted}
    if not pins:
        raise ManifestError("no trusted signer")
    if len(text) > MAX_BYTES:
        raise ManifestError("too large")
    try:
        doc = json.loads(text)
        payload_b64, signatures = doc["payload"], doc["signatures"]
        if not isinstance(payload_b64, str) or not isinstance(signatures, list):
            raise TypeError
    except (ValueError, KeyError, TypeError) as e:
        raise ManifestError("not a signed manifest") from e
    for s in signatures:
        try:
            protected_b64 = s["protected"]
            header = json.loads(unb64u(protected_b64))
            alg = header["alg"]
            leaf = base64.b64decode(header["x5c"][0], validate=True)
            signature = unb64u(s["signature"])
        except (ValueError, KeyError, TypeError, IndexError, ManifestError):
            continue
        if alg not in ALGORITHMS or fingerprint(leaf) not in pins:
            continue
        try:
            cert = x509.load_der_x509_certificate(leaf)
        except ValueError:
            continue
        if _valid(cert, alg, f"{protected_b64}.{payload_b64}".encode("ascii"), signature):
            try:
                payload = json.loads(unb64u(payload_b64))
            except ValueError as e:
                raise ManifestError("bad payload") from e
            if not isinstance(payload, dict) or payload.get("format") != FORMAT:
                raise ManifestError("not an update manifest")
            return payload
    raise ManifestError("no valid signature of a trusted signer")


def sign(payload, key, certs_der, alg):
    """The manifest of [payload] signed with [key]; [certs_der]: leaf first."""
    if alg not in ALGORITHMS:
        raise ManifestError(f"algorithm {alg}")
    header = {"alg": alg, "x5c": [base64.b64encode(d).decode("ascii") for d in certs_der],
              "x5t#S256": b64u(hashlib.sha256(certs_der[0]).digest())}
    protected = b64u(json.dumps(header, separators=(",", ":")).encode())
    body = b64u(json.dumps({"format": FORMAT, "v": 1, **payload}, separators=(",", ":"), ensure_ascii=False).encode())
    data = f"{protected}.{body}".encode("ascii")
    if alg == "ES256":
        r, s = utils.decode_dss_signature(key.sign(data, ec.ECDSA(hashes.SHA256())))
        signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    else:
        signature = key.sign(data, padding.PKCS1v15(), hashes.SHA256())
    return json.dumps({"payload": body, "signatures": [{"protected": protected, "signature": b64u(signature)}]})
