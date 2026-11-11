"""Private technical authorities; no constraint or reverse field on clinical tables."""

import uuid

from django.db import models


class ExpiryAccountState(models.Model):
    user_id = models.BigIntegerField(primary_key=True)
    incarnation = models.UUIDField(default=uuid.uuid4, editable=False)
    scope_revision = models.UUIDField(default=uuid.uuid4, editable=False)
    mutation_epoch = models.UUIDField(default=uuid.uuid4, editable=False)
    generation = models.BigIntegerField(default=0)


class ExpiryPrescriptionState(models.Model):
    prescription = models.OneToOneField(
        "nurse.Prescription",
        primary_key=True,
        db_constraint=False,
        related_name="+",
        on_delete=models.DO_NOTHING,
    )
    revision = models.UUIDField(default=uuid.uuid4, editable=False)
