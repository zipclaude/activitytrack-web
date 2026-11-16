# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Flavio Visentin <flavio-activitytrack@infraconsulting.it>
from django.db import migrations

# the swipes on a running activity that the app's 1.0 no longer has: suspend is no button any more (the app's
# docs/ACTIVITY-MODEL.md §3), and the app reads its own stored SUSPEND and STOP_OR_SUSPEND as STOP
# (EntrySwipe.parse). In a policy it refuses them, and with them the whole policy
KEYS = ("gestureEntryToStart", "gestureEntryToEnd")
DROPPED = ("STOP_OR_SUSPEND", "SUSPEND")
NOW = "STOP"


def map_dropped_values(apps, schema_editor):
    """The policies holding a dropped value get what the app makes of it, and a new revision for the phones."""
    Policy = apps.get_model("policies", "Policy")
    for policy in Policy.objects.all():
        settings = policy.data.get("settings") if isinstance(policy.data, dict) else None
        if not isinstance(settings, dict):
            continue
        changed = False
        for key in KEYS:
            # a setting is a plain value, or {"value": …, "locked"/"hidden": true}
            s = settings.get(key)
            if isinstance(s, dict) and s.get("value") in DROPPED:
                s["value"] = NOW
            elif isinstance(s, str) and s in DROPPED:
                settings[key] = NOW
            else:
                continue
            changed = True
        if changed:
            policy.revision += 1
            policy.save(update_fields=["data", "revision"])


class Migration(migrations.Migration):

    dependencies = [
        ('policies', '0002_policysignature'),
    ]

    operations = [
        migrations.RunPython(map_dropped_values, migrations.RunPython.noop),
    ]
