# SPDX-License-Identifier: GPL-3.0-or-later
"""Raw access for administrators until the web screens exist; edits here bypass the sync clock."""

from django.contrib import admin

from .models import SYNCED_MODELS


class SyncedAdmin(admin.ModelAdmin):
	list_display = ("uid", "__str__", "owner", "rev", "deleted")
	list_filter = ("deleted",)
	readonly_fields = ("uid", "org", "owner", "rev", "clock")

	def has_add_permission(self, request):
		return False

	def has_change_permission(self, request, obj=None):
		return False

	def has_delete_permission(self, request, obj=None):
		return False


for model in SYNCED_MODELS:
	admin.site.register(model, SyncedAdmin)
