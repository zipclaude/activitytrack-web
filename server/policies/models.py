# SPDX-License-Identifier: GPL-3.0-or-later
"""Policies for the phones (docs/ARCHITECTURE.md §7): one per scope, merged key by key."""

from django.conf import settings
from django.db import models

from accounts.models import Profile


class Policy(models.Model):
    """The keys an admin set for the organisation, a profile or one user."""

    org = models.ForeignKey("accounts.Organization", on_delete=models.CASCADE, related_name="+")
    # one of the three: the organisation (both empty), a profile, a user
    profile = models.CharField(max_length=16, choices=Profile.choices, blank=True)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True, related_name="policy")
    # the policy body without format and schema (docs/POLICY.md of the app)
    data = models.JSONField(default=dict, blank=True)
    # bumped at every save: the phones' "version"
    revision = models.PositiveIntegerField(default=0)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["org", "profile"], condition=models.Q(user__isnull=True), name="one_policy_per_scope"),
        ]

    @property
    def scope(self):
        if self.user_id:
            return "user"
        return "profile" if self.profile else "org"
