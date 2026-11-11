"""Real PostgreSQL connections exercise a committed writer during a read."""

import re
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import close_old_connections, connection, transaction

from nurse.models import Prescription
from tests.nurse import test_expiry_reads_v4 as expiry_fixtures
from tests.nurse.test_expiry_read_state import assert_guard_closed
from tests.nurse.test_expiry_reads_v4 import LIST, assert_error

expiry_settings = expiry_fixtures.expiry_settings
expiry_setup = expiry_fixtures.expiry_setup

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql", reason="Real PostgreSQL read/write races"
    ),
]


@pytest.mark.parametrize("writer", ["prescription", "association"])
def test_committed_writer_between_read_queries_never_yields_a_mixed_page(
    expiry_setup, writer
):
    h = expiry_setup
    h.prescription()
    reached, resume = threading.Event(), threading.Event()

    def read():
        close_old_connections()
        paused = False

        def pause_after_clinical_read(execute, sql, params, many, context):
            nonlocal paused
            result = execute(sql, params, many, context)
            if (
                not paused
                and sql.lstrip().upper().startswith("SELECT")
                and re.search(r'\bFROM\s+"?nurse_prescription\b', sql, re.IGNORECASE)
            ):
                paused = True
                reached.set()
                assert resume.wait(10), "Writer did not release paused read"
            return result

        try:
            with connection.execute_wrapper(pause_after_clinical_read):
                return h.client.get(LIST, {"page_size": 100})
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(read)
        try:
            assert reached.wait(10), "Expiry request did not read its clinical set"
            if writer == "prescription":
                h.prescription()
            else:
                h.nurse.patients.remove(h.patient)
        finally:
            resume.set()
        response = future.result(timeout=15)
    assert_error(response, 409, "pagination_invalidated")
    assert Prescription.objects.filter(patient=h.patient).count() == (
        3 if writer == "prescription" else 2
    )


def test_disabled_postgres_guard_closes_reads_until_transaction_rollback(expiry_setup):
    h = expiry_setup
    assert h.client.get(LIST).status_code == 200
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cursor.execute(
                "ALTER TABLE nurse_prescription "
                "DISABLE TRIGGER expiry_prescription_update"
            )
        assert_guard_closed(h)
        transaction.set_rollback(True)
    assert h.client.get(LIST).status_code == 200


def test_truncate_and_same_ids_cannot_revive_revision_or_cursor(expiry_setup):
    h = expiry_setup
    first = Prescription.objects.get(patient=h.patient)
    second = h.prescription()
    before = h.page(page_size=1)
    revision = h.detail(first).json()["item"]["revision"]
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cursor.execute("TRUNCATE nurse_prescription CASCADE")
        recreated = h.prescription(id=first.pk)
        h.prescription(id=second.pk)
        assert h.detail(recreated).json()["item"]["revision"] != revision
        assert_error(
            h.client.get(LIST, {"page_size": 1, "cursor": before["next_cursor"]}),
            409,
            "pagination_invalidated",
        )
        transaction.set_rollback(True)
    assert h.detail(first).json()["item"]["revision"] == revision
