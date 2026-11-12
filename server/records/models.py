# SPDX-License-Identifier: GPL-3.0-or-later
"""
The synced data: one model per record type of docs/SYNC.md §1.

Conventions (mirroring the app's Entities.kt):
- instants are DateTimeFields named *_utc (wire: *UtcMs, epoch milliseconds);
- local dates are DateFields (wire: yyyy-mm-dd);
- local times of day are minutes since midnight (*_min);
- enums are stored by the app's names;
- amounts are EUR cents.
"""

import re
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class Scope(models.TextChoices):
    # owner null = central, else personal; a phone creates personal ones
    MASTER = "master"
    # always owned by one user
    USER = "user"
    # owned as its parent (PARENT names the field)
    CHILD = "child"


class Synced(models.Model):
    uid = models.UUIDField(primary_key=True, default=uuid.uuid4)
    org = models.ForeignKey("accounts.Organization", on_delete=models.PROTECT, related_name="+")
    # null = central master data; children copy their parent's
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    rev = models.BigIntegerField(default=0)
    deleted = models.BooleanField(default=False)
    # field -> time of its last change, epoch ms (docs/SYNC.md §3)
    clock = models.JSONField(default=dict, blank=True)

    SYNC_TYPE = ""
    SCOPE = Scope.USER
    PARENT = None
    # fields the server keeps but phones may not write
    READ_ONLY = ()

    class Meta:
        abstract = True
        indexes = [models.Index(fields=["org", "rev"], name="%(app_label)s_%(class)s_rev")]

    @property
    def is_central(self):
        return self.owner_id is None


def _fk(model, null=True, on_delete=models.PROTECT):
    return models.ForeignKey(model, on_delete=on_delete, null=null, blank=null, related_name="+")


class Company(Synced):
    SYNC_TYPE = "company"
    SCOPE = Scope.MASTER

    name = models.TextField()
    archived = models.BooleanField(default=False)
    color = models.IntegerField(null=True, blank=True)
    external_code = models.TextField(null=True, blank=True)
    position = models.IntegerField(null=True, blank=True)

    class Meta(Synced.Meta):
        verbose_name_plural = "companies"

    def __str__(self):
        return self.name


class Site(Synced):
    label = models.TextField()
    lat = models.FloatField()
    lon = models.FloatField()
    radius_m = models.IntegerField(default=100)
    address = models.TextField(null=True, blank=True)

    class Meta(Synced.Meta):
        abstract = True


class CompanySite(Site):
    SYNC_TYPE = "companySite"
    SCOPE = Scope.CHILD
    PARENT = "company"

    company = _fk(Company, null=False)


class Client(Synced):
    SYNC_TYPE = "client"
    SCOPE = Scope.MASTER

    # null when the policy hides the companies
    company = _fk(Company)
    name = models.TextField()
    archived = models.BooleanField(default=False)
    color = models.IntegerField(null=True, blank=True)
    external_code = models.TextField(null=True, blank=True)
    position = models.IntegerField(null=True, blank=True)

    def __str__(self):
        return self.name


class ClientSite(Site):
    SYNC_TYPE = "clientSite"
    SCOPE = Scope.CHILD
    PARENT = "client"

    client = _fk(Client, null=False)


class Contact(Synced):
    SYNC_TYPE = "contact"
    SCOPE = Scope.CHILD
    PARENT = "client"

    client = _fk(Client, null=False)
    name = models.TextField()
    email = models.TextField(null=True, blank=True)
    external_key = models.TextField(null=True, blank=True)
    # numbers as dialled (the app's contact_phone rows)
    phones = models.JSONField(default=list, blank=True)

    def __str__(self):
        return self.name


class Role(Synced):
    SYNC_TYPE = "role"
    SCOPE = Scope.MASTER

    name = models.TextField()
    archived = models.BooleanField(default=False)
    position = models.IntegerField(null=True, blank=True)

    def __str__(self):
        return self.name


TAG_MAX = 30


def tag_name(text):
    """
    A tag as entries carry it, the app's Tags.normalize: without '#', lower
    case, spaces as '-', letters, digits, '-' and '_' only, at most 30
    characters; "" when nothing is left.
    """
    s = (text or "").strip()
    s = s[1:] if s.startswith("#") else s
    s = re.sub(r"\s+", "-", s.strip().lower())
    s = "".join(c for c in s if c.isalnum() or c in "-_")
    return s.strip("-")[:TAG_MAX]


class Tag(Synced):
    """
    A predefined tag with its colour (app issue #133). Entries keep tags by
    name, so the name is the natural key: unique among the central ones and
    among each user's personal ones.
    """

    SYNC_TYPE = "tag"
    SCOPE = Scope.MASTER

    name = models.TextField()
    # 0xRRGGBB, null = none (a grey dot)
    color = models.IntegerField(null=True, blank=True)
    archived = models.BooleanField(default=False)
    position = models.IntegerField(null=True, blank=True)

    class Meta(Synced.Meta):
        constraints = [
            models.UniqueConstraint(fields=["org", "name"], condition=models.Q(owner__isnull=True, deleted=False), name="records_tag_central_name"),
            models.UniqueConstraint(fields=["org", "owner", "name"], condition=models.Q(owner__isnull=False, deleted=False), name="records_tag_personal_name"),
        ]

    def clean(self):
        self.name = tag_name(self.name)
        if not self.name:
            raise ValidationError({"name": "empty tag"})

    def __str__(self):
        return self.name


class Preset(Synced):
    SYNC_TYPE = "preset"
    SCOPE = Scope.MASTER

    text = models.TextField()
    default_role = _fk(Role)
    company = _fk(Company)
    position = models.IntegerField(null=True, blank=True)


class Material(Synced):
    SYNC_TYPE = "material"
    SCOPE = Scope.MASTER

    name = models.TextField()
    unit = models.TextField(null=True, blank=True)
    unit_cost_cents = models.BigIntegerField(null=True, blank=True)
    company = _fk(Company)
    archived = models.BooleanField(default=False)


class Place(Synced):
    SYNC_TYPE = "place"

    kind = models.CharField(max_length=16, choices=[("HOME", "HOME"), ("OTHER", "OTHER")])
    label = models.TextField()
    lat = models.FloatField()
    lon = models.FloatField()
    radius_m = models.IntegerField(default=100)


class ActivityKind(models.TextChoices):
    ACTIVITY = "ACTIVITY"
    # an absence (docs ACTIVITY-MODEL §3.1): ABSENCE segments only, always CLOSED
    ABSENCE = "ABSENCE"


class EntryStatus(models.TextChoices):
    """The lifecycle of an activity (docs ACTIVITY-MODEL §2.1): a to-do is an activity not started yet."""
    TODO = "TODO"
    RUNNING = "RUNNING"
    CLOSED = "CLOSED"


class AbsenceKind(models.TextChoices):
    ABSENCE = "ABSENCE"
    HOLIDAY = "HOLIDAY"
    LEAVE = "LEAVE"
    SICK = "SICK"


SEGMENT_KINDS = [("WORK", "WORK"), ("TRAVEL", "TRAVEL"), ("PAUSE", "PAUSE"), ("ABSENCE", "ABSENCE")]


class Entry(Synced):
    """
    An activity (the app's time_entry; docs ACTIVITY-MODEL §1): a to-do while
    TODO, then running and closed; an absence when kind is ABSENCE. The name
    stays Entry, as the table and the wire type "entry".
    """

    SYNC_TYPE = "entry"

    contact = _fk(Contact, on_delete=models.SET_NULL)
    company = _fk(Company)
    client = _fk(Client)
    role = _fk(Role)
    activity = models.TextField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=EntryStatus.choices)
    # kind of the open segment; null when none is open
    mode = models.CharField(max_length=16, choices=SEGMENT_KINDS, null=True, blank=True)
    locality = models.TextField(null=True, blank=True)
    location_kind = models.CharField(max_length=16, null=True, blank=True)
    notes = models.TextField(null=True, blank=True)
    zone_id = models.CharField(max_length=64)
    planned_end_utc = models.DateTimeField(null=True, blank=True)
    plugin_fields = models.JSONField(null=True, blank=True)
    tags = models.JSONField(default=list, blank=True)
    # segments overlap after merging two nodes' changes: a person checks (SYNC.md §11)
    needs_review = models.BooleanField(default=False)
    created_utc = models.DateTimeField()
    updated_utc = models.DateTimeField()
    # never changes after creation
    kind = models.CharField(max_length=16, choices=ActivityKind.choices, default=ActivityKind.ACTIVITY)
    # in the bin ("eliminata"): restorable, hidden everywhere else; not the tombstone `deleted`
    deleted_utc = models.DateTimeField(null=True, blank=True)
    # when it was closed: a to-do ticked off has no segment to tell
    closed_utc = models.DateTimeField(null=True, blank=True)
    # key in the management system; unique with the company for each owner
    external_key = models.TextField(null=True, blank=True)
    # deadline ("Scadenza")
    due_date = models.DateField(null=True, blank=True)
    due_time_min = models.IntegerField(null=True, blank=True)
    # plan: a day; a start (else the whole day); a duration (else a point in time)
    plan_date = models.DateField(null=True, blank=True)
    plan_start_min = models.IntegerField(null=True, blank=True)
    duration_min = models.IntegerField(null=True, blank=True)
    # 0 none, 1 low, 2 medium, 3 high, 4 urgent, 5 emergency
    priority = models.IntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(5)])
    # set iff kind is ABSENCE
    absence_kind = models.CharField(max_length=16, choices=AbsenceKind.choices, null=True, blank=True)

    class Meta(Synced.Meta):
        verbose_name_plural = "entries"
        constraints = [
            models.UniqueConstraint(
                fields=["org", "owner", "company", "external_key"],
                condition=models.Q(external_key__isnull=False, deleted=False),
                name="records_entry_external_key",
            ),
        ]

    def clean(self):
        if (self.kind == ActivityKind.ABSENCE) != (self.absence_kind is not None):
            raise ValidationError({"absence_kind": "set for an absence only"})

    def __str__(self):
        return self.activity or str(self.uid)


class ActivityAssignment(models.Model):
    """
    Who gave a TODO activity to its owner (a manager, an admin), on the server
    only: it does not travel, and means something only while the activity is TODO.
    """

    entry = models.OneToOneField(Entry, on_delete=models.CASCADE, primary_key=True, related_name="assignment")
    by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    at = models.DateTimeField(auto_now=True)


class Segment(Synced):
    SYNC_TYPE = "segment"
    SCOPE = Scope.CHILD
    PARENT = "entry"

    entry = _fk(Entry, null=False)
    kind = models.CharField(max_length=16, choices=SEGMENT_KINDS)
    start_utc = models.DateTimeField()
    end_utc = models.DateTimeField(null=True, blank=True)
    zone_id = models.CharField(max_length=64)
    source = models.CharField(max_length=16, default="TRACKED")
    km_estimated = models.FloatField(null=True, blank=True)
    km_declared = models.FloatField(null=True, blank=True)
    note = models.TextField(null=True, blank=True)
    # the plan of a TODO activity: never counted, drawn dashed, deleted on start
    planned = models.BooleanField(default=False)

    def clean(self):
        if (self.planned or self.kind == "ABSENCE") and self.end_utc is None:
            raise ValidationError({"end_utc": "a planned or absence segment is never open"})
        if self.planned and self.kind == "ABSENCE":
            raise ValidationError({"planned": "an absence segment is never planned"})


class Attachment(Synced):
    SYNC_TYPE = "attachment"
    SCOPE = Scope.CHILD
    PARENT = "entry"
    READ_ONLY = ("has_file",)

    entry = _fk(Entry, null=False)
    kind = models.CharField(max_length=16)
    mime = models.CharField(max_length=100)
    transcript = models.TextField(null=True, blank=True)
    created_utc = models.DateTimeField()
    sha256 = models.CharField(max_length=64, blank=True)
    size = models.BigIntegerField(null=True, blank=True)
    # the file arrived (docs/SYNC.md §5 Files)
    has_file = models.BooleanField(default=False)


class Expense(Synced):
    SYNC_TYPE = "expense"
    SCOPE = Scope.CHILD
    PARENT = "entry"

    entry = _fk(Entry, null=False)
    segment = _fk(Segment, on_delete=models.SET_NULL)
    amount_cents = models.BigIntegerField()
    description = models.TextField()
    attachment = _fk(Attachment, on_delete=models.SET_NULL)
    created_utc = models.DateTimeField()


class EntryMaterial(Synced):
    SYNC_TYPE = "entryMaterial"
    SCOPE = Scope.CHILD
    PARENT = "entry"

    entry = _fk(Entry, null=False)
    material = _fk(Material, on_delete=models.SET_NULL)
    description = models.TextField()
    quantity = models.FloatField(null=True, blank=True)
    unit = models.TextField(null=True, blank=True)
    unit_cost_cents = models.BigIntegerField(null=True, blank=True)
    created_utc = models.DateTimeField()


class ExternalRef(Synced):
    SYNC_TYPE = "externalRef"
    SCOPE = Scope.CHILD
    PARENT = "entry"

    entry = _fk(Entry, null=False)
    kind = models.CharField(max_length=16)
    value = models.TextField()


class DayOff(Synced):
    SYNC_TYPE = "dayOff"

    date = models.DateField()
    note = models.TextField(null=True, blank=True)


class WorkSchedule(Synced):
    SYNC_TYPE = "workSchedule"

    day_of_week = models.IntegerField()
    start_min = models.IntegerField()
    end_min = models.IntegerField()


class LunchBreak(Synced):
    SYNC_TYPE = "lunchBreak"

    day_of_week = models.IntegerField()
    start_min = models.IntegerField()
    end_min = models.IntegerField()


class RemoteItem(Synced):
    """A plugin list item (services, activity types…) for the entry form on the web."""

    SYNC_TYPE = "remoteItem"

    company = _fk(Company, null=False)
    plugin_id = models.TextField()
    list = models.TextField()
    parent_id = models.TextField(blank=True)
    item_id = models.TextField()
    name = models.TextField()
    client_code = models.TextField(null=True, blank=True)
    kind = models.TextField(null=True, blank=True)
    active = models.BooleanField(default=True)
    order = models.IntegerField(null=True, blank=True)
    extra = models.JSONField(default=dict, blank=True)


# parents before children: the order of a full push and of the wire schema
SYNCED_MODELS = [
    Company, CompanySite, Client, ClientSite, Contact, Role, Tag, Preset, Material, Place,
    Entry, Segment, Attachment, Expense, EntryMaterial, ExternalRef,
    DayOff, WorkSchedule, LunchBreak, RemoteItem,
]
BY_TYPE = {m.SYNC_TYPE: m for m in SYNCED_MODELS}
