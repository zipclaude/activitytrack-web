# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Flavio Visentin <flavio-activitytrack@infraconsulting.it>
# Wire schema 4: an activity declared complete (app issue #189: one of travel
# only) and the description of an attachment (app issue #185). Both nullable,
# empty on the existing rows.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("records", "0010_drop_stale_scope_exits"),
    ]

    operations = [
        migrations.AddField(
            model_name="entry",
            name="completed_utc",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="attachment",
            name="description",
            field=models.TextField(blank=True, null=True),
        ),
    ]
