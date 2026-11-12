# Activity model 1.0 (the app's docs/ACTIVITY-MODEL.md), first of three steps:
# the new columns, next to the old ones; 0008 moves the data, 0009 drops the
# to-do. Three migrations, three transactions: PostgreSQL refuses to alter a
# table with rows written in the same transaction (deferred foreign keys).

from django.conf import settings
from django.db import migrations, models
import django.core.validators
import django.db.models.deletion

SEGMENT_KINDS = [("WORK", "WORK"), ("TRAVEL", "TRAVEL"), ("PAUSE", "PAUSE"), ("ABSENCE", "ABSENCE")]


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("records", "0006_todo_absence"),
    ]

    operations = [
        migrations.AddField(
            model_name="entry",
            name="kind",
            field=models.CharField(
                choices=[("ACTIVITY", "Activity"), ("ABSENCE", "Absence")], default="ACTIVITY", max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="entry",
            name="deleted_utc",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="entry",
            name="closed_utc",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="entry",
            name="external_key",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="entry",
            name="due_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="entry",
            name="due_time_min",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="entry",
            name="plan_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="entry",
            name="plan_start_min",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="entry",
            name="duration_min",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="entry",
            name="priority",
            field=models.IntegerField(
                default=0,
                validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(5)],
            ),
        ),
        migrations.AddField(
            model_name="entry",
            name="absence_kind",
            field=models.CharField(
                blank=True, max_length=16, null=True,
                choices=[("ABSENCE", "Absence"), ("HOLIDAY", "Holiday"), ("LEAVE", "Leave"), ("SICK", "Sick")],
            ),
        ),
        migrations.AlterField(
            model_name="entry",
            name="status",
            field=models.CharField(
                choices=[("TODO", "Todo"), ("RUNNING", "Running"), ("CLOSED", "Closed")], max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="entry",
            name="mode",
            field=models.CharField(blank=True, choices=SEGMENT_KINDS, max_length=16, null=True),
        ),
        migrations.AddField(
            model_name="segment",
            name="planned",
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name="segment",
            name="kind",
            field=models.CharField(choices=SEGMENT_KINDS, max_length=16),
        ),
        migrations.CreateModel(
            name="ActivityAssignment",
            fields=[
                ("entry", models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE, primary_key=True, related_name="assignment",
                    serialize=False, to="records.entry",
                )),
                ("at", models.DateTimeField(auto_now=True)),
                ("by", models.ForeignKey(
                    null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+",
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
        ),
    ]
