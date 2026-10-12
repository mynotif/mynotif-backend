"""Repair derived search columns after retiring all older writers."""

from django.core.management.base import BaseCommand, CommandError
from django.db import models

from nurse.models import Patient
from nurse.patient_search import SEARCH_COLUMNS, SEARCH_FIELDS, normalize_patient_search


def bounded_rows(queryset, batch_size):
    last_pk = 0
    while True:
        rows = list(
            queryset.filter(pk__gt=last_pk)
            .order_by("pk")
            .values("pk", *SEARCH_FIELDS, *SEARCH_COLUMNS)[:batch_size]
        )
        if not rows:
            return
        yield from rows
        last_pk = rows[-1]["pk"]


def derived_values(row):
    return {
        f"search_{field}": normalize_patient_search(row[field])
        for field in SEARCH_FIELDS
    }


class Command(BaseCommand):
    help = "Repair/check private patient search columns in bounded batches."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=500)
        parser.add_argument("--check", action="store_true")
        parser.add_argument("--database", default="default")

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        if not 1 <= batch_size <= 1000:
            raise CommandError("Batch size must be between 1 and 1000.")
        # Deliberately use a plain queryset: only this trusted maintenance path
        # writes derived columns directly, with all source values as a CAS guard.
        queryset = models.QuerySet(model=Patient, using=options["database"])
        if not options["check"]:
            for row in bounded_rows(queryset, batch_size):
                values = derived_values(row)
                if any(row[column] != value for column, value in values.items()):
                    queryset.filter(
                        pk=row["pk"],
                        **{field: row[field] for field in SEARCH_FIELDS},
                    ).update(**values)
        for row in bounded_rows(queryset, batch_size):
            if any(
                row[column] != value for column, value in derived_values(row).items()
            ):
                raise CommandError(
                    "Search columns are inconsistent; retire older writers and retry."
                )
        self.stdout.write("Patient search columns verified.")
