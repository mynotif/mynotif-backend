"""Independent reservation authority and atomic invalidation, frozen SQL."""

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
    "nurse_expiryrenewal",
    "nurse_expiryaccountstate",
    "nurse_expiryfutureauthority",
    "nurse_pushbinding",
    "nurse_pushdestination",
    "nurse_pushregistrystate",
    "nurse_expiryreminderauthority",
    "nurse_expiryreminderpreference",
)
INVALIDATION_SOURCES = (
    "nurse_expiryfutureauthority",
    "nurse_expiryaccountstate",
    "nurse_expiryreminderpreference",
    "nurse_pushbinding",
    "nurse_pushdestination",
    "nurse_pushregistrystate",
)
EVENTS = ("insert", "update", "delete")
OPPORTUNITY = "nurse_expiryreminderopportunity"
EVENT = "nurse_expiryreminderevent"
ITEM = "nurse_expiryremindereventitem"
ROUTE = "nurse_expiryremindereventroute"
AUTHORITY = "nurse_expiryreminderauthority"


def acquire(postgres):
    return (
        [
            "SET LOCAL lock_timeout = '2s';",
            "DO $$ BEGIN IF NOT pg_try_advisory_xact_lock(28123601) THEN "
            "RAISE EXCEPTION 'Expiry authority busy' USING ERRCODE='55P03'; "
            "END IF; END $$;",
        ]
        if postgres
        else []
    )


def close(postgres):
    random = "gen_random_uuid()" if postgres else "lower(hex(randomblob(16)))"
    closed = "false" if postgres else "0"
    return (
        f"INSERT INTO {AUTHORITY} (id,namespace,accepting) "
        f"VALUES (1,{random},{closed}) "
        f"ON CONFLICT (id) DO UPDATE SET accepting={closed},namespace={random};"
    )


def invalidation(table, event):
    if event == "truncate":
        if table in (
            "nurse_pushbinding",
            "nurse_pushdestination",
            "nurse_pushregistrystate",
        ):
            return f"UPDATE {ROUTE} SET state='cancelled' WHERE state='reserved';"
        predicate = "1=1"
    else:
        refs = {"insert": ("NEW",), "update": ("OLD", "NEW"), "delete": ("OLD",)}[event]
        keys = {
            "nurse_expiryfutureauthority": ("prescription_id", "prescription_id"),
            "nurse_expiryaccountstate": ("user_id", "user_id"),
            "nurse_expiryreminderpreference": ("user_id", "user_id"),
        }
        if table in keys:
            column, key = keys[table]
            predicate = f"{column} IN ({','.join(ref + '.' + key for ref in refs)})"
            if table == "nurse_expiryaccountstate" and event == "update":
                predicate += (
                    " AND (OLD.incarnation<>NEW.incarnation "
                    "OR OLD.scope_revision<>NEW.scope_revision)"
                )
        elif table in (
            "nurse_pushbinding",
            "nurse_pushdestination",
            "nurse_pushregistrystate",
        ):
            if table == "nurse_pushregistrystate":
                predicate = (
                    "OLD.restore_epoch<>NEW.restore_epoch "
                    "OR OLD.accepting<>NEW.accepting"
                    if event == "update"
                    else "1=1"
                )
            elif table == "nurse_pushbinding":
                predicate = f"binding_id IN ({','.join(ref + '.id' for ref in refs)})"
            else:
                identifiers = ",".join(ref + ".id" for ref in refs)
                predicate = (
                    "binding_id IN (SELECT id FROM nurse_pushbinding "
                    f"WHERE destination_id IN ({identifiers}))"
                )
            # Each original route has its own eligibility. Losing one binding
            # neither rearms the event nor withdraws other admitted devices.
            return (
                f"UPDATE {ROUTE} SET state='cancelled' "
                f"WHERE state='reserved' AND ({predicate});"
            )
        else:
            predicate = "1=1"
    affected_events = (
        f"SELECT i.event_id FROM {ITEM} i JOIN {OPPORTUNITY} o "
        f"ON o.id=i.opportunity_id WHERE {predicate}"
    )
    return (
        f"UPDATE {OPPORTUNITY} SET state='cancelled' "
        f"WHERE state IN ('planned','admitted') AND ({predicate});"
        f"UPDATE {EVENT} SET state='cancelled' WHERE state='reserved' "
        f"AND id IN ({affected_events}) AND NOT EXISTS "
        f"(SELECT 1 FROM {ITEM} active_item JOIN {OPPORTUNITY} active_op "
        "ON active_op.id=active_item.opportunity_id "
        f"WHERE active_item.event_id={EVENT}.id "
        "AND active_op.state IN ('planned','admitted'));"
        f"UPDATE {ROUTE} SET state='cancelled' WHERE state='reserved' "
        f"AND event_id IN (SELECT id FROM {EVENT} WHERE state='cancelled' "
        f"AND id IN ({affected_events}));"
    )


def transition(postgres):
    now = "CURRENT_TIMESTAMP"
    return (
        "INSERT INTO nurse_expiryremindertransition "
        "(opportunity_id,from_state,to_state,recorded_at) "
        f"SELECT NEW.id,OLD.state,NEW.state,{now} WHERE OLD.state<>NEW.state;"
    )


def forward_sql(vendor):
    postgres = vendor == "postgresql"
    if vendor not in ("postgresql", "sqlite"):
        raise RuntimeError("Unsupported reminder authority")
    sql = acquire(postgres) + [close(postgres)]
    if postgres:
        sql.append(
            "CREATE FUNCTION reminder_writer_barrier() "
            "RETURNS trigger LANGUAGE plpgsql AS $$ "
            f"BEGIN IF EXISTS (SELECT 1 FROM {AUTHORITY} "
            "WHERE id=1 AND accepting) THEN "
            "IF NOT pg_try_advisory_xact_lock(28123601) THEN "
            "RAISE EXCEPTION 'Expiry authority busy' "
            "USING ERRCODE='55P03'; END IF; END IF; RETURN NULL; END $$;"
        )
    events = (*EVENTS, "truncate") if postgres else EVENTS
    for table in SOURCES:
        for event in events:
            name = f"reminder_barrier_{table}_{event}"
            sql.append(
                f"CREATE TRIGGER {name} BEFORE {event} ON {table} "
                + (
                    "FOR EACH STATEMENT EXECUTE FUNCTION reminder_writer_barrier();"
                    if postgres
                    else "BEGIN SELECT 1; END;"
                )
            )
    for table in INVALIDATION_SOURCES:
        for event in events:
            name = f"reminder_invalidate_{table}_{event}"
            body = invalidation(table, event)
            if postgres:
                timing = "STATEMENT" if event == "truncate" else "ROW"
                sql.extend(
                    [
                        f"CREATE FUNCTION {name}() RETURNS trigger "
                        f"LANGUAGE plpgsql AS $$ BEGIN {body} RETURN NULL; END $$;",
                        f"CREATE TRIGGER {name} AFTER {event} ON {table} "
                        f"FOR EACH {timing} EXECUTE FUNCTION {name}();",
                    ]
                )
            else:
                sql.append(
                    f"CREATE TRIGGER {name} AFTER {event} ON {table} BEGIN {body} END;"
                )
    if postgres:
        sql.extend(
            [
                "CREATE FUNCTION reminder_transition() RETURNS trigger "
                f"LANGUAGE plpgsql AS $$ BEGIN {transition(postgres)} "
                "RETURN NULL; END $$;",
                f"CREATE TRIGGER reminder_transition AFTER UPDATE ON {OPPORTUNITY} "
                "FOR EACH ROW EXECUTE FUNCTION reminder_transition();",
            ]
        )
    else:
        sql.append(
            f"CREATE TRIGGER reminder_transition AFTER UPDATE ON {OPPORTUNITY} "
            f"BEGIN {transition(postgres)} END;"
        )
    return sql


def backward_sql(vendor):
    postgres = vendor == "postgresql"
    sql = acquire(postgres)
    for table in SOURCES:
        for event in (*EVENTS, "truncate") if postgres else EVENTS:
            suffix = f" ON {table}" if postgres else ""
            sql.append(
                f"DROP TRIGGER IF EXISTS reminder_barrier_{table}_{event}{suffix};"
            )
    for table in INVALIDATION_SOURCES:
        for event in (*EVENTS, "truncate") if postgres else EVENTS:
            name = f"reminder_invalidate_{table}_{event}"
            suffix = f" ON {table}" if postgres else ""
            sql.append(f"DROP TRIGGER IF EXISTS {name}{suffix};")
            if postgres:
                sql.append(f"DROP FUNCTION IF EXISTS {name}();")
    suffix = f" ON {OPPORTUNITY}" if postgres else ""
    sql.append(f"DROP TRIGGER IF EXISTS reminder_transition{suffix};")
    if postgres:
        sql.extend(
            [
                "DROP FUNCTION IF EXISTS reminder_transition();",
                "DROP FUNCTION IF EXISTS reminder_writer_barrier();",
            ]
        )
    sql.append(close(postgres))
    return sql


class InstallReminderGuards(migrations.operations.base.Operation):
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
        return "Install reservation barriers and atomic invalidation guards"


class Migration(migrations.Migration):
    dependencies = [("nurse", "0026_expiry_reminders")]
    operations = [InstallReminderGuards()]
