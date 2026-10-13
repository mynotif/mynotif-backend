import unicodedata

from django.db import migrations

SOURCE_FIELDS = ("firstname", "lastname", "city", "phone")


def normalize(value):
    # Frozen algorithm: do not import the runtime helper into old migrations.
    return "".join(
        char
        for char in unicodedata.normalize("NFD", value)
        if not unicodedata.category(char).startswith("M")
    ).casefold()


def backfill_patient_search(apps, schema_editor):
    patient = apps.get_model("nurse", "Patient")
    queryset = patient.objects.using(schema_editor.connection.alias)
    last_pk = 0
    while True:
        rows = list(
            queryset.filter(pk__gt=last_pk)
            .order_by("pk")
            .values("pk", *SOURCE_FIELDS)[:500]
        )
        if not rows:
            return
        for row in rows:
            queryset.filter(
                pk=row["pk"], **{field: row[field] for field in SOURCE_FIELDS}
            ).update(
                **{f"search_{field}": normalize(row[field]) for field in SOURCE_FIELDS}
            )
        last_pk = rows[-1]["pk"]


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("nurse", "0014_patient_search_columns")]
    operations = [
        migrations.RunPython(
            backfill_patient_search, reverse_code=migrations.RunPython.noop
        )
    ]
