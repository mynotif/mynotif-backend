"""Transactional writer coverage for every ORM and SQL path, on both supported DBs.

The SQL is generated here, frozen with the migration. No application imports.
"""

from django.db import migrations

ACCOUNT = "nurse_expiryaccountstate"
RX = "nurse_expiryprescriptionstate"
SOURCES = (
    "auth_user",
    "nurse_prescription",
    "nurse_patient",
    "nurse_nurse",
    "nurse_nurse_patients",
    "nurse_prescriptiondocumentversion",
    "nurse_prescriptiondocumentasset",
    "nurse_documentimport",
)
EVENTS = ("insert", "update", "delete")


def guard_name(table, event):
    return "expiry_" + table.removeprefix("nurse_") + "_" + event


def affected(table, refs):
    """Return affected prescription, patient and direct account SELECTs."""

    def values(field):
        return ",".join(f"{ref}.{field}" for ref in refs)

    ids = values("id")
    if table == "nurse_prescription":
        return (
            f"SELECT id FROM nurse_prescription WHERE id IN ({ids})",
            values("patient_id"),
            None,
        )
    if table == "nurse_patient":
        return (
            f"SELECT id FROM nurse_prescription WHERE patient_id IN ({ids})",
            ids,
            None,
        )
    if table == "nurse_nurse":
        return None, None, values("user_id")
    if table == "auth_user":
        return None, None, ids
    if table == "nurse_nurse_patients":
        return (
            None,
            None,
            f"SELECT user_id FROM nurse_nurse WHERE id IN ({values('nurse_id')})",
        )
    if table == "nurse_prescriptiondocumentversion":
        prescriptions = values("prescription_id")
    elif table == "nurse_documentimport":
        prescriptions = (
            f"SELECT prescription_id FROM nurse_documentimport WHERE id IN ({ids}) "
            f"UNION SELECT prescription_id FROM nurse_prescriptiondocumentversion "
            f"WHERE document_import_id IN ({ids}) UNION SELECT "
            f"{refs[0]}.prescription_id"
        )
        if len(refs) == 2:
            prescriptions += f" UNION SELECT {refs[1]}.prescription_id"
    else:
        prescriptions = (
            "SELECT prescription_id FROM nurse_prescriptiondocumentversion "
            f"WHERE id IN ({values('document_version_id')}) UNION "
            "SELECT prescription_id FROM nurse_documentimport "
            f"WHERE id IN ({values('document_import_id')})"
        )
    rx_query = f"SELECT id FROM nurse_prescription WHERE id IN ({prescriptions})"
    patients = (
        f"SELECT patient_id FROM nurse_prescription WHERE id IN ({prescriptions})"
    )
    return rx_query, patients, None


def row_body(table, event, random_uuid):
    refs = {"insert": ("NEW",), "update": ("OLD", "NEW"), "delete": ("OLD",)}[event]
    rx_query, patients, users = affected(table, refs)
    statements = []
    if table == "auth_user" and event == "insert":
        statements.append(
            f"INSERT INTO {ACCOUNT} "
            f"(user_id,incarnation,scope_revision,mutation_epoch,generation) "
            f"VALUES (NEW.id,{random_uuid},{random_uuid},{random_uuid},0);"
        )
    if table == "nurse_prescription" and event == "insert":
        statements.append(
            f"INSERT INTO {RX} (prescription_id,revision) VALUES "
            f"(NEW.id,{random_uuid});"
        )
    if rx_query:
        statements.append(
            f"UPDATE {RX} SET revision={random_uuid} WHERE prescription_id "
            f"IN ({rx_query});"
        )
    if patients is not None:
        users = (
            "SELECT n.user_id FROM nurse_nurse n JOIN nurse_nurse_patients p "
            f"ON p.nurse_id=n.id WHERE p.patient_id IN ({patients})"
        )
    scope = table in ("auth_user", "nurse_nurse", "nurse_nurse_patients")
    scope_set = (
        f",scope_revision={random_uuid},mutation_epoch={random_uuid}" if scope else ""
    )
    statements.append(
        f"UPDATE {ACCOUNT} SET generation=generation+1{scope_set} WHERE "
        f"user_id IN ({users});"
    )
    if table == "nurse_prescription" and event == "delete":
        statements.append(f"DELETE FROM {RX} WHERE prescription_id=OLD.id;")
    if table == "auth_user" and event == "delete":
        statements.append(f"DELETE FROM {ACCOUNT} WHERE user_id=OLD.id;")
    return "\n".join(statements)


def forward_sql(vendor):
    if vendor not in ("sqlite", "postgresql"):
        raise RuntimeError("Expiry requires PostgreSQL or SQLite writer guards")
    postgres = vendor == "postgresql"
    random_uuid = "gen_random_uuid()" if postgres else "lower(hex(randomblob(16)))"
    sql = []
    if postgres:
        sql += ["SET LOCAL lock_timeout = '2s';"]
        # Hold the same brief writer lock as CREATE TRIGGER while initializing.
        sql += [f"LOCK TABLE {','.join(SOURCES)} IN SHARE ROW EXCLUSIVE MODE;"]
    for table in SOURCES:
        for event in EVENTS:
            name = guard_name(table, event)
            body = row_body(table, event, random_uuid)
            if postgres:
                sql += [
                    f"CREATE FUNCTION {name}() RETURNS trigger LANGUAGE "
                    f"plpgsql AS $$ BEGIN\n"
                    f"{body}\nRETURN NULL; END $$;",
                    f"CREATE TRIGGER {name} AFTER {event.upper()} ON {table} "
                    f"FOR EACH ROW EXECUTE FUNCTION {name}();",
                ]
            else:
                sql.append(
                    f"CREATE TRIGGER {name} AFTER {event.upper()} ON {table} "
                    f"BEGIN\n{body}\nEND;"
                )
    if postgres:
        sql.append(
            "CREATE FUNCTION expiry_truncate() RETURNS trigger LANGUAGE "
            "plpgsql AS $$ BEGIN "
            f"DELETE FROM {RX} WHERE prescription_id NOT IN "
            "(SELECT id FROM nurse_prescription); "
            f"DELETE FROM {ACCOUNT} WHERE user_id NOT IN (SELECT id FROM auth_user); "
            f"UPDATE {ACCOUNT} SET "
            f"generation=generation+1,scope_revision=gen_random_uuid(),"
            "mutation_epoch=gen_random_uuid(); "
            f"UPDATE {RX} SET revision=gen_random_uuid(); RETURN NULL; END $$;"
        )
        for table in SOURCES:
            sql.append(
                f"CREATE TRIGGER {guard_name(table, 'truncate')} AFTER "
                f"TRUNCATE ON {table} "
                "FOR EACH STATEMENT EXECUTE FUNCTION expiry_truncate();"
            )
    sql += [
        f"INSERT INTO {ACCOUNT} "
        f"(user_id,incarnation,scope_revision,mutation_epoch,generation) "
        f"SELECT id,{random_uuid},{random_uuid},{random_uuid},0 FROM auth_user;",
        f"INSERT INTO {RX} (prescription_id,revision) SELECT "
        f"id,{random_uuid} FROM nurse_prescription;",
    ]
    return sql


def backward_sql(vendor):
    sql = ["SET LOCAL lock_timeout = '2s';"] if vendor == "postgresql" else []
    for table in SOURCES:
        for event in (*EVENTS, "truncate") if vendor == "postgresql" else EVENTS:
            name = guard_name(table, event)
            suffix = f" ON {table}" if vendor == "postgresql" else ""
            sql.append(f"DROP TRIGGER IF EXISTS {name}{suffix};")
            if vendor == "postgresql" and event != "truncate":
                sql.append(f"DROP FUNCTION IF EXISTS {name}();")
    if vendor == "postgresql":
        sql.append("DROP FUNCTION IF EXISTS expiry_truncate();")
    # Reverse/forward rotates all authorities, so an old cursor cannot revive.
    sql += [f"DELETE FROM {RX};", f"DELETE FROM {ACCOUNT};"]
    return sql


class InstallExpiryGuards(migrations.operations.base.Operation):
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
        return "Install transactional expiry writer guards and initialize authorities"


class Migration(migrations.Migration):
    dependencies = [("nurse", "0020_expiry_read_state"), ("main", "0003_auth_sessions")]
    operations = [InstallExpiryGuards()]
