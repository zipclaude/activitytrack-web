# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import secrets
import uuid
from datetime import timedelta

from django.contrib.auth.models import AbstractUser, UserManager
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class Organization(models.Model):
    """One per installation today; every record carries it (docs/ARCHITECTURE.md §4)."""

    name = models.CharField(_("nome"), max_length=200)
    created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Profile(models.TextChoices):
    # "role" is master data in the app: permissions are profiles (ARCHITECTURE.md §5)
    ADMIN = "admin", _("Amministratore")
    MANAGER = "manager", _("Responsabile")
    USER = "user", _("Utente")
    READER = "reader", _("Lettore azienda")


class User(AbstractUser):
    org = models.ForeignKey(Organization, on_delete=models.PROTECT, null=True, related_name="users")
    profile = models.CharField(_("profilo"), max_length=16, choices=Profile.choices, default=Profile.USER)
    # the manager whose team this user is in
    manager = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="team")
    # a reader: the companies whose closed entries they see; a manager: the companies
    # they are responsible for, whose entries they see whoever did them
    companies = models.ManyToManyField(
        "records.Company", blank=True, related_name="readers", verbose_name=_("aziende"),
        help_text=_("Lettore azienda: le attività chiuse di queste aziende. Responsabile: tutte le attività fatte per queste aziende, oltre a quelle del suo team."))

    objects = UserManager()

    @property
    def is_admin(self):
        return self.profile == Profile.ADMIN or self.is_superuser

    @property
    def can_edit_central(self):
        return self.is_admin or self.profile == Profile.MANAGER

    @property
    def syncs(self):
        """Readers have no data of their own and no phones."""
        return self.profile != Profile.READER


def _hash(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


class DeviceManager(models.Manager):
    def by_token(self, token):
        return self.select_related("user", "user__org").filter(token_hash=_hash(token), revoked__isnull=True).first()


class Device(models.Model):
    """A paired phone; its bearer token is kept only as a hash."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="devices")
    name = models.CharField(_("nome"), max_length=100)
    model = models.CharField(_("modello"), max_length=100, blank=True)
    app_version = models.CharField(_("versione app"), max_length=40, blank=True)
    token_hash = models.CharField(max_length=64, unique=True)
    fcm_token = models.TextField(blank=True)
    # the one phone of the user that sends to the management systems (ARCHITECTURE.md §8)
    plugin_holder = models.BooleanField(_("invia ai gestionali"), default=False)
    created = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    cursor = models.BigIntegerField(default=0)
    revoked = models.DateTimeField(null=True, blank=True)

    objects = DeviceManager()

    def __str__(self):
        return f"{self.name} ({self.user})"

    @classmethod
    def create_for(cls, user, **fields):
        """The new device and its token, returned once."""
        token = secrets.token_urlsafe(32)
        holder = not user.devices.filter(revoked__isnull=True, plugin_holder=True).exists()
        device = cls.objects.create(user=user, token_hash=_hash(token), plugin_holder=holder, **fields)
        return device, token


class PairingCode(models.Model):
    """A one-time code shown as a QR code (docs/SYNC.md §9)."""

    TTL = timedelta(minutes=10)

    code_hash = models.CharField(max_length=64, unique=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="pairing_codes")
    created = models.DateTimeField(auto_now_add=True)
    expires = models.DateTimeField()
    device = models.OneToOneField(Device, on_delete=models.SET_NULL, null=True, blank=True)

    @classmethod
    def issue(cls, user):
        code = secrets.token_urlsafe(20)
        pairing = cls.objects.create(user=user, code_hash=_hash(code), expires=timezone.now() + cls.TTL)
        return pairing, code

    @classmethod
    def redeem(cls, code):
        """The pending pairing of this code, locked for the caller's transaction; None if unusable."""
        return (
            cls.objects.select_for_update()
            .select_related("user")
            .filter(code_hash=_hash(code), device__isnull=True, expires__gt=timezone.now())
            .first()
        )
