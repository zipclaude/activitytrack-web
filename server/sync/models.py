# SPDX-License-Identifier: GPL-3.0-or-later
from django.conf import settings
from django.db import models


class Counter(models.Model):
    """The organisation's revision counter (docs/SYNC.md §4)."""

    org = models.OneToOneField("accounts.Organization", on_delete=models.CASCADE, primary_key=True)
    value = models.BigIntegerField(default=0)


class ScopeExit(models.Model):
    """A record that left a user's scope: pulled as "gone" (docs/SYNC.md §5)."""

    org = models.ForeignKey("accounts.Organization", on_delete=models.CASCADE, related_name="+")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    type = models.CharField(max_length=32)
    uid = models.UUIDField()
    rev = models.BigIntegerField()

    class Meta:
        indexes = [models.Index(fields=["user", "rev"])]
