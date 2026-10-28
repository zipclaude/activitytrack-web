# SPDX-License-Identifier: GPL-3.0-or-later
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import Device, Organization, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
	fieldsets = BaseUserAdmin.fieldsets + (("ActivityTrack", {"fields": ("org", "profile", "manager", "companies")}),)
	list_display = ("username", "first_name", "last_name", "profile", "manager", "is_active")
	list_filter = ("profile", "is_active")
	filter_horizontal = ("companies",)


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
	list_display = ("name", "user", "model", "app_version", "plugin_holder", "last_seen", "revoked")
	list_filter = ("revoked",)
	readonly_fields = ("token_hash", "cursor", "created", "last_seen")


admin.site.register(Organization)
