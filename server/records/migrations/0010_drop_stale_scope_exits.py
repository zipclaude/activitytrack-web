# The to-do and the absence are no wire types any more (activity model 1.0), but
# the scope exits recorded for them stay in the table. A pull from 0, which every
# device makes after its migration, returned them as "gone" entries of a type the
# app does not know: it refused them and its pull never got past them. The rows
# go; the exits of the types that still exist stay.

from django.db import migrations


def forward(apps, schema_editor):
    apps.get_model("sync", "ScopeExit").objects.filter(type__in=("todo", "absence")).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("records", "0009_drop_todo"),
        ("sync", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
