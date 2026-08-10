"""
Repair a partially applied inward.0001_initial migration on MySQL.

Run from the Django backend root with:

    python manage.py shell -c "exec(open('repair_inward_initial_migration.py', encoding='utf-8').read())"

Safety rules:
- vendors.0001_initial must already be applied.
- inward.0001_initial must NOT be recorded as applied.
- every existing inward_* table must contain zero rows.
- the script aborts without deleting anything if any inward_* table has data.
"""

from django.core.management import call_command
from django.db import connection
from django.db.migrations.recorder import (
    MigrationRecorder,
)


VENDORS_MIGRATION = (
    "vendors",
    "0001_initial",
)

INWARD_MIGRATION = (
    "inward",
    "0001_initial",
)


def quote_table_name(table_name):
    return connection.ops.quote_name(
        table_name,
    )


recorder = MigrationRecorder(
    connection,
)

applied_migrations = set(
    recorder.applied_migrations(),
)

print("Checking migration state...")

if (
    VENDORS_MIGRATION
    not in applied_migrations
):
    raise RuntimeError(
        "vendors.0001_initial is not applied. "
        "Run `python manage.py migrate vendors` first."
    )

if (
    INWARD_MIGRATION
    in applied_migrations
):
    print(
        "inward.0001_initial is already recorded "
        "as applied. No repair is required."
    )
else:
    all_tables = (
        connection.introspection
        .table_names()
    )

    inward_tables = sorted(
        table_name
        for table_name in all_tables
        if table_name.startswith(
            "inward_",
        )
    )

    print(
        "Existing inward tables:",
        inward_tables or "none",
    )

    table_row_counts = {}

    with connection.cursor() as cursor:
        for table_name in inward_tables:
            cursor.execute(
                "SELECT COUNT(*) FROM "
                f"{quote_table_name(table_name)}"
            )

            table_row_counts[
                table_name
            ] = int(
                cursor.fetchone()[0]
            )

    print(
        "Inward table row counts:",
        table_row_counts,
    )

    non_empty_tables = {
        table_name: row_count
        for (
            table_name,
            row_count,
        ) in table_row_counts.items()
        if row_count > 0
    }

    if non_empty_tables:
        raise RuntimeError(
            "Repair stopped because one or more "
            "inward tables contain data: "
            f"{non_empty_tables}. "
            "Do not drop or fake the migration. "
            "Back up and reconcile the existing "
            "schema manually."
        )

    if inward_tables:
        print(
            "Removing empty orphan tables "
            "left by the failed migration..."
        )

        with connection.cursor() as cursor:
            cursor.execute(
                "SET FOREIGN_KEY_CHECKS = 0"
            )

            try:
                for table_name in reversed(
                    inward_tables
                ):
                    print(
                        "Dropping:",
                        table_name,
                    )

                    cursor.execute(
                        "DROP TABLE IF EXISTS "
                        f"{quote_table_name(table_name)}"
                    )
            finally:
                cursor.execute(
                    "SET FOREIGN_KEY_CHECKS = 1"
                )

    print(
        "Applying inward.0001_initial normally..."
    )

    call_command(
        "migrate",
        "inward",
        "0001",
        verbosity=1,
        interactive=False,
    )

    print(
        "Applying remaining migrations..."
    )

    call_command(
        "migrate",
        verbosity=1,
        interactive=False,
    )

    print(
        "Migration repair completed successfully."
    )