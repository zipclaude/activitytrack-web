# SPDX-License-Identifier: GPL-3.0-or-later
"""What an organisation's server knows about new versions (docs/SYNC.md §12.4)."""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class UpdateSettings(models.Model):
    """Where to look, how often and what to do with the packages; and the manifest accepted last."""

    org = models.OneToOneField("accounts.Organization", on_delete=models.CASCADE, related_name="update_settings")
    enabled = models.BooleanField(_("controlla gli aggiornamenti"), default=True)
    manifest_url = models.CharField(_("indirizzo del manifesto"), max_length=500)
    # channels the organisation follows, comma separated: prod, beta, test, dev, …
    channels = models.CharField(max_length=100, default="prod")
    poll_hours = models.PositiveSmallIntegerField(default=12)
    mirror = models.BooleanField(default=True)
    last_checked = models.DateTimeField(null=True, blank=True)
    last_ok = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    # the manifest accepted last: its payload, issue time and highest version (no downgrade, no replay)
    payload = models.JSONField(null=True, blank=True)
    issued = models.CharField(max_length=40, blank=True)
    top_version = models.CharField(max_length=60, blank=True)
    # the highest version seen per channel: a followed channel may not go back (a channel nobody follows, or
    # one a withdrawn release left, never blocks a manifest)
    tops = models.JSONField(default=dict, blank=True)
    # the server's own signed manifest for the phones, kept until something it depends on changes
    relayed = models.TextField(blank=True)
    relay_key = models.CharField(max_length=64, blank=True)
    # the leaf fingerprint it was signed for: another key (a certificate configured later) signs again
    relay_signer = models.CharField(max_length=64, blank=True)

    @classmethod
    def for_org(cls, org):
        """The organisation's row, made with the settings.ini defaults the first time."""
        found = cls.objects.filter(org=org).first()
        if found:
            return found
        row, _made = cls.objects.get_or_create(org=org, defaults={
            "manifest_url": settings.UPDATES_MANIFEST_URL, "channels": settings.UPDATES_CHANNELS,
            "poll_hours": settings.UPDATES_POLL_HOURS})
        return row

    def channel_list(self):
        return [c.strip() for c in self.channels.split(",") if c.strip()]


class AcceptedSigner(models.Model):
    """A key, besides the app's, whose manifests the server accepts: pasted by an admin as a fingerprint."""

    org = models.ForeignKey("accounts.Organization", on_delete=models.CASCADE, related_name="accepted_signers")
    fingerprint = models.CharField(max_length=64)
    label = models.CharField(max_length=80, blank=True)
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    added = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["added"]
        constraints = [models.UniqueConstraint(fields=["org", "fingerprint"], name="one_signer_per_fingerprint")]


class MirrorFile(models.Model):
    """A package the server keeps for its phones: the file is `<files>/updates/<sha256>`."""

    org = models.ForeignKey("accounts.Organization", on_delete=models.CASCADE, related_name="mirror_files")
    version = models.CharField(max_length=60)
    component = models.CharField(max_length=40)
    sha256 = models.CharField(max_length=64)
    size = models.PositiveBigIntegerField(default=0)
    fetched = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org", "version", "component"], name="one_mirror_file_per_package")]
