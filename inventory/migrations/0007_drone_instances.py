from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0003_serial_purchase_costs"),
    ]

    operations = [
        migrations.CreateModel(
            name="DroneInstance",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sequence", models.PositiveIntegerField()),
                ("instance_code", models.CharField(db_index=True, max_length=100, unique=True)),
                ("status", models.CharField(choices=[
                    ("AVAILABLE", "Available"),
                    ("SALE_PENDING", "Sale Pending"),
                    ("SOLD", "Sold"),
                    ("RETURNABLE_PENDING", "Returnable Pending"),
                    ("RETURNABLE_ACTIVE", "Returnable Active"),
                    ("RETURN_QC_PENDING", "Return QC Pending"),
                    ("QC_FAILED", "QC Failed"),
                    ("SCRAP_PENDING", "Scrap Pending"),
                    ("SCRAPPED", "Scrapped"),
                    ("SCRAPPED_REORDERED", "Scrapped - Reordered"),
                ], db_index=True, default="AVAILABLE", max_length=30)),
                ("workflow_metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("material_request", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="drone_instances", to="materialrequest.materialrequest")),
                ("replacement_material_request", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="replacement_for_drone_instances", to="materialrequest.materialrequest")),
            ],
            options={"ordering": ["material_request_id", "sequence"]},
        ),
        migrations.CreateModel(
            name="DroneComponentAllocation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity", models.PositiveIntegerField(default=0)),
                ("serial_numbers", models.JSONField(blank=True, default=list)),
                ("source_details", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("component", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="drone_component_allocations", to="components.component")),
                ("drone_instance", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="component_allocations", to="inventory.droneinstance")),
            ],
            options={"ordering": ["drone_instance_id", "component_id"]},
        ),
        migrations.AddConstraint(
            model_name="droneinstance",
            constraint=models.UniqueConstraint(fields=("material_request", "sequence"), name="uniq_drone_instance_mr_seq"),
        ),
        migrations.AddIndex(
            model_name="droneinstance",
            index=models.Index(fields=["material_request", "status"], name="drone_mr_status_idx"),
        ),
        migrations.AddConstraint(
            model_name="dronecomponentallocation",
            constraint=models.UniqueConstraint(fields=("drone_instance", "component"), name="uniq_drone_instance_component"),
        ),
        migrations.AddIndex(
            model_name="dronecomponentallocation",
            index=models.Index(fields=["drone_instance", "component"], name="drone_inst_comp_idx"),
        ),
    ]
