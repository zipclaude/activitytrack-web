# SPDX-License-Identifier: GPL-3.0-or-later
"""The manifest the server hands to its devices: the accepted one, signed by the server (docs/SYNC.md §12.4)."""

import copy
import hashlib
import json
import logging

from . import jws, keys
from .models import MirrorFile

log = logging.getLogger("updates")


def path(sha256):
    """Where a device fetches a mirrored package, relative to the server's address."""
    return f"/api/v1/update/files/{sha256}"


def payload(cfg):
    """The accepted payload as the devices get it: with the mirror on, a package of a followed channel
    is served by the server (its address replaced) and is left out while the server does not have it."""
    out = copy.deepcopy(cfg.payload)
    if not cfg.mirror:
        return out
    channels = cfg.channel_list()
    have = {(m.version, m.component): m.sha256 for m in MirrorFile.objects.filter(org=cfg.org)}
    releases = []
    for r in out["releases"]:
        if (r.get("channel") or "prod") in channels:
            packages = []
            for p in r["packages"]:
                if have.get((r["version"], p["component"])) == p["sha256"]:
                    p["url"] = path(p["sha256"])
                    packages.append(p)
            r["packages"] = packages
        if r["packages"]:
            releases.append(r)
    out["releases"] = releases
    return out


def rebuild(cfg):
    """Sign again when what it depends on changed; the text is kept on the row."""
    if not cfg.payload:
        return ""
    try:
        alg, signer = keys.usable(), keys.fingerprint()
        if alg is None:
            raise ValueError("the signing key is neither P-256 nor RSA")
        body = payload(cfg)
        key = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        if cfg.relayed and cfg.relay_key == key and cfg.relay_signer == signer:
            return cfg.relayed
        private, certs = keys.load()
        cfg.relayed = jws.sign(body, private, certs, alg)
        cfg.relay_key, cfg.relay_signer = key, signer
        cfg.save(update_fields=["relayed", "relay_key", "relay_signer"])
    except Exception:
        log.exception("updates: the manifest for the devices could not be signed")
        return ""
    return cfg.relayed


def text(cfg):
    """What `head` carries: the signed manifest, or None. Signed again if the key changed meanwhile."""
    if not cfg.payload:
        return None
    if cfg.relayed:
        try:
            if cfg.relay_signer == keys.fingerprint():
                return cfg.relayed
        except Exception:
            return None
    return rebuild(cfg) or None
