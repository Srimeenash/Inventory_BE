from django.db import migrations


def add_removed_from_inventory(apps, schema_editor):
    table_name = 'inward_inwardentry'
    column_name = 'removed_from_inventory'
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SHOW COLUMNS FROM `%s` LIKE %%s" % table_name,
            [column_name],
        )
        if cursor.fetchone() is None:
            cursor.execute(
                "ALTER TABLE `%s` ADD COLUMN `%s` tinyint(1) NOT NULL DEFAULT 0" % (table_name, column_name)
            )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('inward', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(add_removed_from_inventory, reverse_code=noop),
    ]
