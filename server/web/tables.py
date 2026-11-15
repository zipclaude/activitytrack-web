# SPDX-License-Identifier: GPL-3.0-or-later
"""
The tables of the reports (issues #62, #63): columns that can be sorted and chosen, in the page, in the print
and in the exports. A table is rows (dicts of what the report knows) and columns; a column knows how to show a
row's value in the page (a cell), how to sort it and which plain values it exports. The sort, the direction and
the visible columns are in the URL (?sort=work&dir=desc&col=date&col=work…), and the columns are also kept in
the session for the next time (as the Home's filters are).
"""

import csv
import io
import json
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from django.http import HttpResponse
from django.utils import timezone

FORMATS = ("csv", "xml", "json")


def cell(text="", sub="", href="", html=None, title=""):
    """What a cell shows: a text (or markup), a small text under it, a link."""
    return {"text": text, "sub": sub, "href": href, "html": html, "title": title}


@dataclass
class Column:
    key: str
    label: object
    # row -> cell(); row -> value to sort by (None last); row -> list of the plain values of `fields`
    show: callable
    sort: callable
    raw: callable
    # (key, label, unit) of each plain value this column exports
    fields: tuple = ()
    right: bool = False
    default: bool = True
    # rows -> cell() of the footer, or None
    total: callable = None


@dataclass
class Table:
    name: str
    columns: list
    rows: list
    sort: str = ""
    direction: str = "asc"
    chosen: list = field(default_factory=list)

    @property
    def visible(self):
        return [c for c in self.columns if c.key in self.chosen]


def _remembered(request):
    return request.session.get("report_columns", {})


def choose(request, name, columns, defaults=None):
    """The visible columns: the form's (?f=1&col=…), else the last chosen for [name], else the default ones."""
    keys = [c.key for c in columns]
    default = [c.key for c in columns if (c.key in defaults if defaults is not None else c.default)]
    g = request.GET
    if g.get("f"):
        asked = [k for k in g.getlist("col") if k in keys]
        # no column is no choice: the default ones
        saved = _remembered(request)
        if asked:
            saved[name] = asked
        else:
            saved.pop(name, None)
        request.session["report_columns"] = saved
        return [k for k in keys if k in asked] or default
    kept = [k for k in _remembered(request).get(name, []) if k in keys]
    return [k for k in keys if k in kept] or default


def order(rows, columns, key, direction):
    """The rows sorted by the column [key], stably; those without a value stay last in either direction."""
    column = next((c for c in columns if c.key == key), None)
    if column is None:
        return rows
    withs = [(column.sort(r), i, r) for i, r in enumerate(rows)]
    full = sorted((x for x in withs if x[0] is not None), key=lambda x: x[0], reverse=direction == "desc")
    return [r for _v, _i, r in full] + [r for v, _i, r in withs if v is None]


def build(request, name, columns, rows, defaults=None):
    g = request.GET
    sort = g.get("sort") if g.get("sort") in {c.key for c in columns} else ""
    direction = "desc" if g.get("dir") == "desc" else "asc"
    chosen = choose(request, name, columns, defaults)
    visible = [c for c in columns if c.key in chosen]
    return Table(name, columns, order(rows, visible, sort, direction), sort, direction, chosen)


def page(request, table):
    """What the template draws: the head with the links that sort, the rows of cells, the footer."""
    query = request.GET.copy()
    for k in ("sort", "dir"):
        query.pop(k, None)
    head = []
    for c in table.visible:
        mine = table.sort == c.key
        nxt = "desc" if mine and table.direction == "asc" else "asc"
        q = query.copy()
        q["sort"], q["dir"] = c.key, nxt
        head.append({"key": c.key, "label": c.label, "right": c.right, "href": "?" + q.urlencode(), "sorted": mine,
                     "direction": table.direction if mine else "",
                     "aria": ("ascending" if table.direction == "asc" else "descending") if mine else "none"})
    rows = [[c.show(r) for c in table.visible] for r in table.rows]
    foot = [c.total(table.rows) if c.total else None for c in table.visible]
    return {"head": head, "rows": rows, "foot": foot if any(foot) else None,
            "numeric": [i for i, c in enumerate(table.visible) if c.right]}


def flat(table):
    """[(key, label, unit)] and the rows of plain values of the visible columns, as they are shown."""
    fields = [f for c in table.visible for f in c.fields]
    return fields, [[v for c in table.visible for v in c.raw(r)] for r in table.rows]


def _text(value):
    return "" if value is None else str(value)


def export(table, fmt, meta, safe):
    """
    The table as a download: CSV (semicolon, with the byte order mark spreadsheets want, free text guarded
    by [safe] against formulas), XML or JSON. The values are plain: ISO dates, 24-hour times, durations in
    minutes, money in euros with a point; the head says the unit.
    """
    fields, rows = flat(table)
    stamp = timezone.localtime().strftime("%Y%m%d")
    filename = f"activitytrack-{meta['report']}-{meta['from']}-{meta['to']}"
    if fmt == "csv":
        out = io.StringIO()
        w = csv.writer(out, delimiter=";")
        w.writerow([f"{label} ({unit})" if unit else str(label) for _k, label, unit in fields])
        for r in rows:
            w.writerow([safe(_text(v)) for v in r])
        response = HttpResponse("﻿" + out.getvalue(), content_type="text/csv; charset=utf-8")
    elif fmt == "json":
        body = {**meta, "generated": stamp,
                "columns": [{"key": k, "label": str(label), **({"unit": unit} if unit else {})}
                            for k, label, unit in fields],
                "rows": [{k: v for (k, _l, _u), v in zip(fields, r)} for r in rows]}
        text = json.dumps(body, ensure_ascii=False, indent=1) + "\n"
        response = HttpResponse(text, content_type="application/json; charset=utf-8")
    else:
        root = ET.Element("report", {k: _text(v) for k, v in meta.items()})
        cols = ET.SubElement(root, "columns")
        for k, label, unit in fields:
            ET.SubElement(cols, "column", {"key": k, "label": str(label), **({"unit": unit} if unit else {})})
        body = ET.SubElement(root, "rows")
        for r in rows:
            row = ET.SubElement(body, "row")
            for (k, _l, _u), v in zip(fields, r):
                ET.SubElement(row, k).text = _text(v)
        ET.indent(root)
        xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
        response = HttpResponse(xml, content_type="application/xml; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}.{fmt}"'
    return response
