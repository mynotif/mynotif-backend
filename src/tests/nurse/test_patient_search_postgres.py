"""Synthetic qualification of legacy RLS, without adding or changing policies."""

import pytest
from django.db import connection, transaction
from psycopg2.sql import SQL, Identifier

from nurse.models import Nurse, Patient

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL only"),
]


def test_legacy_force_rls_without_policy_denies_role_and_bypass_keeps_orm_scope(user):
    nurse = Nurse.objects.create(user=user)
    own = Patient.objects.create(firstname="Élodie", lastname="Synthetic", city="Lyon")
    foreign = Patient.objects.create(firstname="Élodie", lastname="Other", city="Lyon")
    nurse.patients.add(own)
    other_user = type(user).objects.create_user(username="m03-267-b@example.invalid")
    Nurse.objects.create(user=other_user).patients.add(foreign)
    role = "m03_267_synthetic_no_bypass"
    with connection.cursor() as cursor:
        cursor.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        if not cursor.fetchone()[0]:
            pytest.skip(
                "Synthetic role qualification requires an isolated superuser DB."
            )
        cursor.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid = 'nurse_patient'::regclass"
        )
        original_rls, original_force = cursor.fetchone()
        cursor.execute(
            SQL("CREATE ROLE {} NOSUPERUSER NOBYPASSRLS NOLOGIN").format(
                Identifier(role)
            )
        )
        cursor.execute(
            SQL("GRANT USAGE ON SCHEMA public TO {}").format(Identifier(role))
        )
        cursor.execute(
            SQL(
                "GRANT SELECT ON nurse_patient, nurse_nurse, nurse_nurse_patients TO {}"
            ).format(Identifier(role))
        )
    try:
        # Reproduce the existing migration's FORCE RLS/no-policy behavior on
        # the synthetic table only; fresh installs may run it before contenttypes.
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("ALTER TABLE nurse_patient ENABLE ROW LEVEL SECURITY")
                cursor.execute("ALTER TABLE nurse_patient FORCE ROW LEVEL SECURITY")
                cursor.execute(SQL("SET LOCAL ROLE {}").format(Identifier(role)))
                cursor.execute(
                    "SELECT rolsuper, rolbypassrls FROM pg_roles "
                    "WHERE rolname = current_user"
                )
                assert cursor.fetchone() == (False, False)
            assert (
                Patient.objects.filter(nurse__user=user)
                .search_normalized("elodie lyon")
                .count()
                == 0
            )
        # The isolated synthetic superuser bypasses RLS, yet ORM authorization
        # still admits only the attached patient's normalized result.
        assert set(Patient.objects.search_normalized("elodie lyon")) == {own, foreign}
        assert list(
            Patient.objects.filter(nurse__user=user).search_normalized("elodie lyon")
        ) == [own]
        assert list(
            Patient.objects.filter(nurse__user=other_user).search_normalized(
                "elodie lyon"
            )
        ) == [foreign]
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
            cursor.execute(
                "ALTER TABLE nurse_patient "
                + ("ENABLE" if original_rls else "DISABLE")
                + " ROW LEVEL SECURITY"
            )
            cursor.execute(
                "ALTER TABLE nurse_patient "
                + ("FORCE" if original_force else "NO FORCE")
                + " ROW LEVEL SECURITY"
            )
            cursor.execute(
                SQL(
                    "REVOKE SELECT ON nurse_patient, nurse_nurse, nurse_nurse_patients "
                    "FROM {}"
                ).format(Identifier(role))
            )
            cursor.execute(
                SQL("REVOKE USAGE ON SCHEMA public FROM {}").format(Identifier(role))
            )
            cursor.execute(SQL("DROP ROLE {}").format(Identifier(role)))
