# SPDX-License-Identifier: GPL-3.0-or-later
"""
The app's policy catalog (docs/policy-catalog.json of the app, copied here
as policy-catalog.json): the editor's form and the checks of what it saves.
Copy the file again whenever the app's catalog changes.
"""

import json
from functools import cache
from pathlib import Path

FILE = Path(__file__).with_name("policy-catalog.json")


@cache
def catalog():
    return json.loads(FILE.read_text(encoding="utf-8"))


def setting(key):
    return next((s for s in catalog()["settings"] if s["key"] == key), None)


def check_value(spec, value):
    """The error of a setting value, None when fine."""
    kind = spec["kind"]
    if kind == "BOOLEAN":
        return None if isinstance(value, bool) else "true/false"
    if kind in ("INT", "DOUBLE"):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or (kind == "INT" and not isinstance(value, int)):
            return "number"
        if "allowed" in spec and value not in spec["allowed"]:
            return "one of " + ", ".join(map(str, spec["allowed"]))
        if "min" in spec and not spec["min"] <= value <= spec["max"]:
            return f"{spec['min']}–{spec['max']}"
        return None
    if not isinstance(value, str):
        return "text"
    if "allowed" in spec:
        values = [v.strip() for v in value.split(",") if v.strip()] if spec.get("multiple") else [value]
        bad = [v for v in values if v not in spec["allowed"]]
        if bad:
            return "one of " + ", ".join(spec["allowed"])
    return None


# keys of the catalog without "since" are as old as the sync (the app's 0.9.0)
FIRST = "0.9.0"
SECTIONS = ("features", "fields", "settings", "behaviours")


def since(section, key):
    """The app version that knows [section].[key] (the catalog's "since")."""
    entry = next((e for e in catalog().get(section, []) if e.get("key") == key), None)
    return (entry or {}).get("since", FIRST)


def unknown_to_phones(data, devices):
    """
    [(key, since, [device])] of the keys of a policy body that some of the
    paired phones' apps do not know yet: such an app refuses the whole policy
    and keeps its previous one (docs/SYNC.md §12).
    """
    from sync.compat import version_key

    out = []
    for section in SECTIONS:
        for key in (data.get(section) or {}):
            needed = since(section, key)
            old = [d for d in devices if d.app_version and version_key(d.app_version) < version_key(needed)]
            if old:
                out.append((f"{section}.{key}", needed, old))
    return out
