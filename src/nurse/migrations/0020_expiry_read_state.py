"""Add only technical tables, with no FK or column on a clinical table."""

import uuid

import django.db.models.deletion
from django.db import migrations, models


class CreateExpiryState(migrations.CreateModel):
    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == "postgresql":
            schema_editor.execute("SET LOCAL lock_timeout = '2s'")
        super().database_backwards(app_label, schema_editor, from_state, to_state)


class Migration(migrations.Migration):
    dependencies = [("nurse", "0019_push_registry")]
    operations = [
        CreateExpiryState(
            name="ExpiryAccountState",
            fields=[
                ("user_id", models.BigIntegerField(primary_key=True, serialize=False)),
                ("incarnation", models.UUIDField(default=uuid.uuid4, editable=False)),
                (
                    "scope_revision",
                    models.UUIDField(default=uuid.uuid4, editable=False),
                ),
                (
                    "mutation_epoch",
                    models.UUIDField(default=uuid.uuid4, editable=False),
                ),
                ("generation", models.BigIntegerField(default=0)),
            ],
        ),
        CreateExpiryState(
            name="ExpiryPrescriptionState",
            fields=[
                (
                    "prescription",
                    models.OneToOneField(
                        to="nurse.prescription",
                        primary_key=True,
                        serialize=False,
                        db_constraint=False,
                        related_name="+",
                        on_delete=django.db.models.deletion.DO_NOTHING,
                    ),
                ),
                ("revision", models.UUIDField(default=uuid.uuid4, editable=False)),
            ],
        ),
    ]
