# SPDX-License-Identifier: GPL-3.0-or-later
"""What the phones are offered (docs/SYNC.md §5, docs/OFFLINE-MAPS.md): the catalog and its revision."""

import hashlib
import json

SCHEMA = 1


def areas(org):
    """The areas with a good file, when the organisation offers offline maps at all."""
    if not org.offline_maps:
        return []
    return [a for a in org.map_areas.order_by("slug") if a.usable and a.built_bbox and a.path.exists()]


def entries(org):
    return [{
        "id": a.slug, "name": a.name, "file": a.file_name, "bytes": a.size, "sha256": a.sha256,
        "bbox": a.built_bbox, "maxZoom": a.built_zoom, "updated": a.built.date().isoformat() if a.built else "",
    } for a in areas(org)]


def revision(org):
    """A short fingerprint of the catalog: the phones fetch it again only when it changes; empty without maps."""
    e = entries(org)
    if not e:
        return ""
    return hashlib.sha256(json.dumps(e, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def document(org):
    e = entries(org)
    return {"schema": SCHEMA, "rev": revision(org), "maps": e}


def area_for(org, slug):
    return next((a for a in areas(org) if a.slug == slug), None)
