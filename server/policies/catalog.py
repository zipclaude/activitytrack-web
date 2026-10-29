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
