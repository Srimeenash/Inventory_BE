from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("materialrequest", "0006_alter_bomitem_unit_alter_rditem_unit_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="materialrequest",
            name="returnable_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="materialrequest",
            name="source_drone_movement_id",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="materialrequest",
            name="source_drone_mr",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="linked_component_returnables",
                to="materialrequest.materialrequest",
            ),
        ),
    ]
