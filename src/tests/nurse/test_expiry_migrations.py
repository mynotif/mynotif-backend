"""Additive upgrade and reverse/forward on disposable databases only."""

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from rest_framework.test import APIClient

from main.session_services import login
from nurse.models import Prescription
from tests.nurse import test_expiry_reads_v4 as expiry_fixtures
from tests.nurse.test_expiry_reads_v4 import LIST, PASSWORD

pytestmark = pytest.mark.django_db(transaction=True)
expiry_settings = expiry_fixtures.expiry_settings
OLD = ("nurse", "0019_push_registry")
NEW = ("nurse", "0021_expiry_read_guards")


def migrate(target):
    executor = MigrationExecutor(connection)
    executor.migrate([target])
    return executor.loader.project_state([target]).apps


def test_upgrade_backfills_existing_accounts_and_prescriptions_without_changing_dates(
    expiry_settings,
):
    old_apps = migrate(OLD)
    try:
        user = User.objects.create_user(
            username="upgrade280@example.invalid", password=PASSWORD
        )
        patient = old_apps.get_model("nurse", "Patient").objects.create(
            firstname="Avant", lastname="Migration"
        )
        nurse = old_apps.get_model("nurse", "Nurse").objects.create(user_id=user.pk)
        nurse.patients.add(patient)
        historical = old_apps.get_model("nurse", "Prescription")
        invalid = historical.objects.create(
            patient_id=patient.pk,
            prescribing_doctor="Historique",
            start_date="2027-01-01",
            end_date="2020-02-29",
            photo_prescription="",
        )
        unknown = historical.objects.create(
            patient_id=patient.pk,
            prescribing_doctor="Inconnu",
            start_date=None,
            end_date=None,
            photo_prescription="",
        )
        migrate(NEW)
        pair = login(user.username, PASSWORD)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {pair['access_token']}")
        response = client.get(LIST)
        assert response.status_code == 200
        rows = {item["id"]: item for item in response.json()["results"]}
        assert response.json()["count"] == 2
        assert rows[invalid.pk]["date_state"] == "invalid"
        assert (rows[invalid.pk]["start_date"], rows[invalid.pk]["end_date"]) == (
            "2027-01-01",
            "2020-02-29",
        )
        assert rows[unknown.pk]["date_state"] == "unknown"
        assert rows[unknown.pk]["start_date"] is rows[unknown.pk]["end_date"] is None
        revisions = [item["revision"] for item in rows.values()]
        assert all(isinstance(value, str) and value for value in revisions)
        before = response.json()["context"]["collection_revision"]
        Prescription.objects.filter(pk=invalid.pk).update(start_date=None)
        assert client.get(LIST).json()["context"]["collection_revision"] != before

        # Reverse/forward retains clinical sources and recreates functioning guards.
        migrate(OLD)
        original = historical.objects.get(pk=invalid.pk)
        assert original.start_date is None
        assert str(original.end_date) == "2020-02-29"
        migrate(NEW)
        before = client.get(LIST).json()["context"]["collection_revision"]
        Prescription.objects.filter(pk=invalid.pk).update(end_date=None)
        after = client.get(LIST)
        assert after.status_code == 200 and after.json()["count"] == 2
        assert after.json()["context"]["collection_revision"] != before
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
