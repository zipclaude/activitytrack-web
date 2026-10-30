# SPDX-License-Identifier: GPL-3.0-or-later
"""What a web user sees (docs/ARCHITECTURE.md §5) and the numbers the pages show."""

from datetime import datetime, time, timedelta

from django.db.models import Q
from django.utils import timezone

from accounts.models import Profile, User
from records.models import Client, Company, Entry, EntryStatus, Expense, Role, Segment, Todo


def own_or_central(user):
    return Q(owner=user) | Q(owner__isnull=True)


def masters(user, model):
    """Master data a user picks from: central and their own, live ones."""
    return model.objects.filter(org=user.org, deleted=False).filter(own_or_central(user))


def companies(user):
    return masters(user, Company).filter(archived=False).order_by("name")


def clients(user):
    return masters(user, Client).filter(archived=False).select_related("company").order_by("company__name", "name")


def roles(user):
    return masters(user, Role).filter(archived=False).order_by("name")


def people(user):
    """Whose entries a user may read: own; a manager their team too; an admin everyone."""
    if user.is_admin:
        return User.objects.filter(org=user.org).exclude(profile=Profile.READER)
    if user.profile == Profile.MANAGER:
        return User.objects.filter(Q(pk=user.pk) | Q(manager=user), org=user.org)
    return User.objects.filter(pk=user.pk)


def readable_entries(user):
    q = Entry.objects.filter(org=user.org, deleted=False).select_related("company", "client", "role", "owner")
    if user.profile == Profile.READER:
        # a client company's administration: closed entries of its companies only
        return q.filter(company__in=user.companies.all(), status=EntryStatus.CLOSED)
    return q.filter(owner__in=people(user))


def writable(user, rec):
    return rec.owner_id == user.pk


def owner_times_fixed_for(user):
    """The user's policy locks "editable times" off: on the web too they only add travel and pauses."""
    from policies.effective import fixed_setting

    return fixed_setting(user, "editableTimes") is False


def owner_times_fixed(entry):
    return owner_times_fixed_for(entry.owner)


def can_fix_times(user, entry):
    """
    Who edits an entry's segments: its owner unless the policy locks the
    times; an admin, or the owner's manager, always (they fix what a
    locked phone cannot, e.g. segments a sync left overlapping).
    """
    if entry.owner_id == user.pk:
        return not owner_times_fixed(entry)
    return (user.is_admin or user.profile == Profile.MANAGER) and people(user).filter(pk=entry.owner_id).exists()


def todos(user):
    return Todo.objects.filter(org=user.org, deleted=False, owner=user).select_related("company", "client")


def day_bounds(day):
    tz = timezone.get_current_timezone()
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


def durations(segments, now=None):
    """Minutes per kind, the open segment up to now."""
    now = now or timezone.now()
    out = {"WORK": 0, "TRAVEL": 0, "PAUSE": 0}
    for s in segments:
        end = s.end_utc or now
        out[s.kind] = out.get(s.kind, 0) + max(0, int((end - s.start_utc).total_seconds() // 60))
    return out


def segments_of(entries):
    by = {}
    for s in Segment.objects.filter(entry__in=entries, deleted=False).order_by("start_utc"):
        by.setdefault(s.entry_id, []).append(s)
    return by


def expenses_of(entries):
    by = {}
    for x in Expense.objects.filter(entry__in=entries, deleted=False):
        by.setdefault(x.entry_id, []).append(x)
    return by


def hm(minutes):
    return f"{minutes // 60}:{minutes % 60:02d}"

