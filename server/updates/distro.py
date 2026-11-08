# SPDX-License-Identifier: GPL-3.0-or-later
"""The manifest's component of this server's own distribution (`web-deb13`, `web-ubuntu24.04`, …; docs/SYNC.md §12.4)."""

from pathlib import Path

OS_RELEASE = "/etc/os-release"
# Debian testing has no VERSION_ID: its codename tells
DEBIAN = {"13": "deb13", "14": "deb14", "trixie": "deb13", "forky": "deb14"}


def os_release(path=OS_RELEASE):
    """The KEY=value lines of os-release(5) as a dict; empty when the file cannot be read."""
    out = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and not line.startswith("#"):
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def suffix(info):
    """"deb13", "deb14", "ubuntu24.04", "ubuntu26.04", or None for a distribution without a package of ours."""
    ident = info.get("ID", "").lower()
    if ident == "debian":
        return DEBIAN.get(info.get("VERSION_ID", "")) or DEBIAN.get(info.get("VERSION_CODENAME", "").lower())
    if ident == "ubuntu" and info.get("VERSION_ID") in ("24.04", "26.04"):
        return "ubuntu" + info["VERSION_ID"]
    return None


def component(name="web", path=OS_RELEASE):
    """`<name>-<distribution>` for this machine, or None."""
    found = suffix(os_release(path))
    return f"{name}-{found}" if found else None
