"""Renewal admission barriers; clinical projection SQL belongs to0024."""

import importlib

from django.db import migrations

schema = importlib.import_module("nurse.migrations.0024_expiry_renewals")


def forward_sql(vendor):
    if vendor not in ("sqlite", "postgresql"):
        raise RuntimeError("Unsupported renewal authority")
    postgres = vendor == "postgresql"
    sql = schema.acquire(postgres) + schema.close_authority(postgres)
    if postgres:
        sql.append(f"LOCK TABLE {schema.RELATION} IN SHARE ROW EXCLUSIVE MODE;")
    sql += schema.projection_install_sql(vendor)
    for event in (*schema.EVENTS, "truncate") if postgres else schema.EVENTS:
        name = f"expiry_cmd_{schema.RELATION}_{event}"
        if postgres:
            sql.append(
                f"CREATE TRIGGER {name} BEFORE {event} ON {schema.RELATION} "
                "FOR EACH STATEMENT EXECUTE FUNCTION expiry_command_barrier();"
            )
        else:
            sql.append(
                f"CREATE TRIGGER {name} BEFORE {event} ON {schema.RELATION} "
                "BEGIN SELECT 1; END;"
            )
    return sql


def backward_sql(vendor):
    postgres = vendor == "postgresql"
    sql = schema.acquire(postgres) + schema.close_authority(postgres)
    for event in (*schema.EVENTS, "truncate") if postgres else schema.EVENTS:
        suffix = f" ON {schema.RELATION}" if postgres else ""
        # Guard-only rollback keeps clinical projections. Full0024 rollback
        # archives the clinical table and removes its dependent AFTER triggers.
        sql.append(
            f"DROP TRIGGER IF EXISTS expiry_cmd_{schema.RELATION}_{event}{suffix};"
        )
    return sql


class InstallRenewalGuards(migrations.operations.base.Operation):
    reversible = True
    reduces_to_sql = True

    def state_forwards(self, app_label, state):
        pass

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        for sql in forward_sql(schema_editor.connection.vendor):
            schema_editor.execute(sql)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        for sql in backward_sql(schema_editor.connection.vendor):
            schema_editor.execute(sql)

    def describe(self):
        return "Install two-object renewal projection and shared writer authority"


class Migration(migrations.Migration):
    dependencies = [("nurse", "0024_expiry_renewals")]
    operations = [InstallRenewalGuards()]
