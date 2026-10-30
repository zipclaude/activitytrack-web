# SPDX-License-Identifier: GPL-3.0-or-later
"""What a web user sees (docs/ARCHITECTURE.md §5) and the numbers the pages show."""

from datetime import datetime, time, timedelta

from django.db.models import Q
from django.utils import timezone

from accounts.models import Profile, User
from records.models import Client, Company, Entry, EntryStatus, Expense, Role, Segment, Tag, Todo


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


def tags(user):
    """The tags a user picks from: central and their own, live ones."""
    return masters(user, Tag).filter(archived=False).order_by("position", "name")


def tag_colors(user):
    """name -> 0xRRGGBB or None of every tag the user sees; a central one wins over a personal one of the same name."""
    out = {}
    for name, color, owner in masters(user, Tag).order_by("-owner").values_list("name", "color", "owner"):
        if owner is None or name not in out:
            out[name] = color
    return out


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


def assignable(user):
    """Who a user gives to-dos to: a manager their team, an admin everyone with data; themselves always."""
    if user.is_admin:
        q = User.objects.filter(org=user.org).exclude(profile=Profile.READER)
    elif user.profile == Profile.MANAGER:
        q = User.objects.filter(Q(pk=user.pk) | Q(manager=user), org=user.org)
    else:
        q = User.objects.filter(pk=user.pk)
    return q.filter(is_active=True).order_by("first_name", "username")


def team_todos(user):
    """The to-dos a user reads and edits: their own and those of whom they assign to."""
    return Todo.objects.filter(org=user.org, deleted=False, owner__in=assignable(user)).select_related("company", "client", "owner", "assignment__by")


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
