# Activity model 1.0, third step: the to-do, its assignment, the entries' link
# to it and the legacy absences go; an activity's management-system key is
# unique with its company for each owner.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("records", "0008_activity_data"),
    ]

    operations = [
        migrations.RemoveField(model_name="entry", name="todo"),
        migrations.DeleteModel(name="TodoAssignment"),
        migrations.DeleteModel(name="Todo"),
        migrations.DeleteModel(name="Absence"),
        migrations.AddConstraint(
            model_name="entry",
            constraint=models.UniqueConstraint(
                condition=models.Q(("deleted", False), ("external_key__isnull", False)),
                fields=("org", "owner", "company", "external_key"),
                name="records_entry_external_key",
            ),
        ),
    ]
