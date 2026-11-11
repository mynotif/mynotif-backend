"""Shared writer authority and durable future revision gate, frozen SQL."""

from django.db import migrations

SOURCES = (
    "auth_user",
    "nurse_prescription",
    "nurse_patient",
    "nurse_nurse",
    "nurse_nurse_patients",
    "nurse_prescriptiondocumentversion",
    "nurse_prescriptiondocumentasset",
    "nurse_documentimport",
    "main_authsession",
    "main_authtoken",
    "nurse_expirycommandauthority",
)
EVENTS = ("insert", "update", "delete")
AUTHORITY_LOCK = 28123601
FUTURE = "nurse_expiryfutureauthority"
RX = "nurse_expiryprescriptionstate"


def forward_sql(vendor):
    postgres = vendor == "postgresql"
    if vendor not in ("sqlite", "postgresql"):
        raise RuntimeError("Unsupported correction authority")
    sql = []
    if postgres:
        sql += [
            "SET LOCAL lock_timeout = '2s';",
            f"DO $$ BEGIN IF NOT pg_try_advisory_xact_lock({AUTHORITY_LOCK}) THEN "
            "RAISE EXCEPTION 'Expiry authority busy' USING ERRCODE='55P03'; "
            "END IF; END $$;",
            f"LOCK TABLE {','.join((*SOURCES, RX))} IN SHARE ROW EXCLUSIVE MODE;",
            "CREATE FUNCTION expiry_command_barrier() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN IF EXISTS (SELECT 1 FROM nurse_expirycommandauthority "
            "WHERE id=1 AND accepting) THEN "
            f"IF NOT pg_try_advisory_xact_lock({AUTHORITY_LOCK}) THEN "
            "RAISE EXCEPTION 'Expiry authority busy' USING ERRCODE='55P03'; "
            "END IF; END IF; RETURN NULL; END $$;",
        ]
        for table in SOURCES:
            for event in (*EVENTS, "truncate"):
                sql.append(
                    f"CREATE TRIGGER expiry_cmd_{table}_{event} "
                    f"BEFORE {event} ON {table} "
                    "FOR EACH STATEMENT EXECUTE FUNCTION expiry_command_barrier();"
                )
    # SQLite's writer serialization is the barrier, including direct SQL writers.
    # These guards attest coverage; they never change legacy write semantics.
    else:
        for table in SOURCES:
            for event in EVENTS:
                sql.append(
                    f"CREATE TRIGGER expiry_cmd_{table}_{event} "
                    f"BEFORE {event} ON {table} "
                    "BEGIN SELECT 1; END;"
                )
    for event in EVENTS:
        ref = "OLD" if event == "delete" else "NEW"
        revision = (
            ("gen_random_uuid()" if postgres else "lower(hex(randomblob(16)))")
            if event == "delete"
            else f"{ref}.revision"
        )
        body = (
            f"INSERT INTO {FUTURE} (prescription_id,revision,generation) "
            f"VALUES ({ref}.prescription_id,{revision},1) "
            "ON CONFLICT (prescription_id) DO UPDATE SET revision=excluded.revision,"
            f"generation={FUTURE}.generation+1;"
        )
        name = f"expiry_cmd_future_{event}"
        if postgres:
            sql += [
                f"CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN {body} RETURN NULL; END $$;",
                f"CREATE TRIGGER {name} AFTER {event} ON {RX} FOR EACH ROW "
                f"EXECUTE FUNCTION {name}();",
            ]
        else:
            sql.append(f"CREATE TRIGGER {name} AFTER {event} ON {RX} BEGIN {body} END;")
    sql += [
        f"INSERT INTO {FUTURE} (prescription_id,revision,generation) "
        f"SELECT prescription_id,revision,0 FROM {RX} WHERE 1=1 "
        "ON CONFLICT (prescription_id) DO UPDATE SET revision=excluded.revision,"
        f"generation={FUTURE}.generation+1;",
        "INSERT INTO nurse_expirycommandauthority (id,namespace,accepting) VALUES "
        "(1,"
        + ("gen_random_uuid(),false" if postgres else "lower(hex(randomblob(16))),0")
        + ") ON CONFLICT (id) DO UPDATE SET accepting="
        + ("false" if postgres else "0")
        + ";",
        "UPDATE nurse_expiryaccountstate SET mutation_epoch="
        + ("gen_random_uuid()" if postgres else "lower(hex(randomblob(16)))")
        + ";",
    ]
    return sql


def backward_sql(vendor):
    postgres = vendor == "postgresql"
    sql = (
        [
            "SET LOCAL lock_timeout = '2s';",
            f"DO $$ BEGIN IF NOT pg_try_advisory_xact_lock({AUTHORITY_LOCK}) THEN "
            "RAISE EXCEPTION 'Expiry authority busy' USING ERRCODE='55P03'; "
            "END IF; END $$;",
        ]
        if postgres
        else []
    )
    for table in SOURCES:
        for event in (*EVENTS, "truncate") if postgres else EVENTS:
            suffix = f" ON {table}" if postgres else ""
            sql.append(f"DROP TRIGGER IF EXISTS expiry_cmd_{table}_{event}{suffix};")
    for event in EVENTS:
        name = f"expiry_cmd_future_{event}"
        suffix = f" ON {RX}" if postgres else ""
        sql.append(f"DROP TRIGGER IF EXISTS {name}{suffix};")
        if postgres:
            sql.append(f"DROP FUNCTION IF EXISTS {name}();")
    if postgres:
        sql.append("DROP FUNCTION IF EXISTS expiry_command_barrier();")
    sql += [
        "UPDATE nurse_expirycommandauthority SET accepting="
        + ("false" if postgres else "0")
        + ";",
        "UPDATE nurse_expiryaccountstate SET mutation_epoch="
        + ("gen_random_uuid()" if postgres else "lower(hex(randomblob(16)))")
        + ";",
    ]
    return sql


class InstallCommandGuards(migrations.operations.base.Operation):
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
        return "Install correction authority and future revision writer guards"


class Migration(migrations.Migration):
    dependencies = [("nurse", "0022_expiry_commands")]
    operations = [InstallCommandGuards()]
