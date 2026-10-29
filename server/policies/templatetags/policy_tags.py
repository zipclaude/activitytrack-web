# SPDX-License-Identifier: GPL-3.0-or-later
from django import template

register = template.Library()


@register.filter
def get(mapping, key):
    """mapping[key], empty when missing (form values by computed names)."""
    value = mapping.get(key, "") if hasattr(mapping, "get") else ""
    return "" if value is None else value


@register.filter
def has(values, item):
    """item is one of values: a list, or a comma text."""
    if isinstance(values, str):
        values = [v.strip() for v in values.split(",")]
    return str(item) in [str(v) for v in (values or [])]


@register.filter
def same(a, b):
    return str(a) == str(b)
