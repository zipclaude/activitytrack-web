# SPDX-License-Identifier: GPL-3.0-or-later
"""
The key the server signs with (docs/SYNC.md §12.4): the one of `[policy] key` and `cert`, else
one made on first use under the data folder: a P-256 key and a self-signed certificate valid ten
years. Its leaf's SHA-256 fingerprint goes in the pairing link (`k`): that is how the PC apps and
the phones know whom to trust, a CA is not needed.
"""

import datetime
import fcntl
import os
import socket
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID
from django.conf import settings

from . import jws

YEARS = 10


def common_name():
    """The CN of the generated certificate: the server's address as the phones know it. The phones pin it with
    the first signed policy (docs/POLICY.md §2), so it must not change: it comes from `base_url`, not from
    whatever the machine is called this year."""
    host = urlsplit(settings.BASE_URL).hostname or socket.gethostname()
    return f"ActivityTrack Web {host}"[:64]


def signing_dir():
    return Path(settings.DATA_DIR) / "signing"


def _write(path, data, mode):
    """Atomic: a reader never sees half a file."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def generate(directory):
    """The key and certificate in [directory] (made once; several processes may ask at the same time)."""
    directory.mkdir(parents=True, exist_ok=True)
    key_path, cert_path = directory / "server-key.pem", directory / "server-cert.pem"
    with open(directory / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if key_path.exists() and cert_path.exists():
            return key_path, cert_path
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name())])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=365 * YEARS + 3))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        _write(key_path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()), 0o600)
        _write(cert_path, cert.public_bytes(serialization.Encoding.PEM), 0o644)
    return key_path, cert_path


def files():
    """(key, certificate, chain) paths in use; chain is "" when there is none."""
    if settings.POLICY_KEY and settings.POLICY_CERT:
        return Path(settings.POLICY_KEY), Path(settings.POLICY_CERT), settings.POLICY_CHAIN
    key, cert = generate(signing_dir())
    return key, cert, ""


_cache = {}


def load():
    """(private key, certificates in DER, leaf first); read again only when a file changed (`head` asks often)."""
    paths = [p for p in files() if p]
    stamp = tuple((str(p), Path(p).stat().st_mtime_ns) for p in paths)
    if _cache.get("stamp") != stamp:
        key_path, cert_path, chain_path = files()
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        certs = []
        for path in [cert_path] + ([Path(chain_path)] if chain_path else []):
            certs += [c.public_bytes(serialization.Encoding.DER) for c in x509.load_pem_x509_certificates(path.read_bytes())]
        _cache.update(stamp=stamp, value=(key, certs))
    return _cache["value"]


def fingerprint():
    """The leaf's SHA-256, lowercase hex: what the pairing link's `k` carries."""
    return jws.fingerprint(load()[1][0])


def usable():
    """The algorithm clients accept for the key in use ("ES256", "RS256"), else None."""
    key = load()[0]
    if isinstance(key, ec.EllipticCurvePrivateKey) and key.curve.name == "secp256r1":
        return "ES256"
    return "RS256" if isinstance(key, rsa.RSAPrivateKey) else None
