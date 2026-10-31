# SPDX-License-Identifier: GPL-3.0-or-later
"""
Compatibility between app and server versions (docs/SYNC.md §12).

The wire schema is a number that grows only when what travels changes
(types, fields, their meaning). Within a major release the server speaks
its schema and the two before it (owner, 2026-10-31): a change from an
older app is brought up to the current schema, a record for it is brought
down to its schema, step by step through STEPS. Outside the window the
server answers 426 with what to update.

The app names its version and schema in two headers on every request; an
app without them (0.9.x before the rule) speaks schema 1.
"""

from dataclasses import dataclass
from typing import Callable

# the current wire schema; raise it with a Step for the previous one
SCHEMA = 1
# the earlier schemas the server still speaks
WINDOW = 2


@dataclass(frozen=True)
class Step:
    """From schema n to n + 1 (`up`, a change of an older app) and back (`down`, a record for it)."""
    up: Callable[[dict], dict]
    down: Callable[[dict], dict]


# n -> the step between schema n and n + 1; every schema in the window needs one
STEPS: dict[int, Step] = {}

APP_HEADER = "X-ActivityTrack-App"
SCHEMA_HEADER = "X-ActivityTrack-Schema"


def min_schema():
    return max(1, SCHEMA - WINDOW)


def client(request):
    """(app version, schema) the request names; schema 1 without the header."""
    version = request.headers.get(APP_HEADER, "")[:40]
    try:
        schema = int(request.headers.get(SCHEMA_HEADER, "1"))
    except ValueError:
        schema = 0
    return version, schema


def refusal(schema):
    """None when the server speaks [schema], else what has to be updated."""
    if schema < min_schema():
        return "app_too_old"
    if schema > SCHEMA:
        return "server_too_old"
    return None


def describe():
    """What the server speaks, in the pair and head answers."""
    from atweb import __version__

    return {"server": __version__, "schema": SCHEMA, "minSchema": min_schema()}


def to_client(record, schema):
    """A record of the current schema, as an app of [schema] reads it."""
    for n in range(SCHEMA - 1, schema - 1, -1):
        record = STEPS[n].down(record)
    return record


def from_client(change, schema):
    """A change of an app of [schema], in the current schema."""
    for n in range(schema, SCHEMA):
        change = STEPS[n].up(change)
    return change


def version_key(text):
    """"1.2.3", "1.2.3-beta.1" -> a sortable tuple; a pre-release before its release; unknown text first."""
    main, _, pre = (text or "").partition("-")
    try:
        numbers = tuple(int(x) for x in main.split("."))
    except ValueError:
        return (-1,)
    numbers = (numbers + (0, 0, 0))[:3]
    return numbers + ((0, pre) if pre else (1, ""))
