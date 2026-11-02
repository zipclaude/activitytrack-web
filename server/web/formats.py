# SPDX-License-Identifier: GPL-3.0-or-later
"""
Dates and times as the user prefers them (issue #38): European, American or
ISO dates, 24 or 12 hours. The pages read the formats of the request's user
through a context variable, set by the middleware below; the forms keep ISO
values (the pickers in pickers.js show them in these formats).
"""

from contextvars import ContextVar

from django.template.defaultfilters import date as _date

EU, US, ISO = "eu", "us", "iso"
H24, H12 = "24", "12"

# Django date formats per style
DATES = {
    EU: {"date": "d/m/Y", "day": "D j/m", "short": "j/m", "long": "l j F Y", "month_day": "j M", "month_day_year": "j M Y"},
    US: {"date": "m/d/Y", "day": "D n/j", "short": "n/j", "long": "l, F j, Y", "month_day": "M j", "month_day_year": "M j, Y"},
    ISO: {"date": "Y-m-d", "day": "D m-d", "short": "m-d", "long": "l Y-m-d", "month_day": "m-d", "month_day_year": "Y-m-d"},
}
TIMES = {H24: "H:i", H12: "g:i A"}

_current = ContextVar("formats", default=(EU, H24))


def current():
    return _current.get()


def middleware(get_response):
    def handle(request):
        user = getattr(request, "user", None)
        token = _current.set((user.date_format, user.time_format) if user is not None and user.is_authenticated else (EU, H24))
        try:
            return get_response(request)
        finally:
            _current.reset(token)
    return handle


def date(value, style="date"):
    """A date or datetime (already local) in the user's format."""
    if value in (None, ""):
        return ""
    return _date(value, DATES[current()[0]][style])


def time(value):
    """The time of a datetime (already local) in the user's format."""
    if value in (None, ""):
        return ""
    return _date(value, TIMES[current()[1]])


def clock(minutes):
    """Minutes from midnight as a time of day in the user's format."""
    if minutes is None:
        return ""
    h, m = minutes // 60 % 24, minutes % 60
    if current()[1] == H12:
        return f"{(h + 11) % 12 + 1}:{m:02d} {'AM' if h < 12 else 'PM'}"
    return f"{h:02d}:{m:02d}"
