"""Durable correction reconciliation, separate from the clinical history."""

import uuid

from django.db import models
from django.utils import timezone


class ExpiryCommandAuthority(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    namespace = models.UUIDField(default=uuid.uuid4)
    accepting = models.BooleanField(default=False)


class ExpiryCommandJournal(models.Model):
    user_id = models.BigIntegerField(primary_key=True)
    principal_incarnation = models.UUIDField()
    revision = models.UUIDField(default=uuid.uuid4)


class ExpiryCommand(models.Model):
    command_id = models.UUIDField(primary_key=True, editable=False)
    user_id = models.BigIntegerField()
    principal_incarnation = models.UUIDField()
    namespace = models.UUIDField()
    origin_session_id = models.UUIDField()
    mutation_epoch = models.UUIDField()
    kind = models.CharField(max_length=32)
    state = models.CharField(max_length=16, default="pending")
    recorded_at = models.DateTimeField(default=timezone.now)
    terminal_at = models.DateTimeField(null=True)
    payload_ciphertext = models.BinaryField(null=True)
    key_version = models.CharField(max_length=64)
    payload_hmac = models.CharField(max_length=64)
    patient_id = models.BigIntegerField()
    prescription_id = models.BigIntegerField()
    result = models.JSONField(null=True)
    error = models.JSONField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("prescription_id",),
                condition=models.Q(state="pending"),
                name="expiry_one_pending_prescription",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state="pending",
                        terminal_at__isnull=True,
                        result__isnull=True,
                        error__isnull=True,
                        payload_ciphertext__isnull=False,
                    )
                    | models.Q(
                        state="succeeded",
                        terminal_at__isnull=False,
                        result__isnull=False,
                        error__isnull=True,
                        payload_ciphertext__isnull=True,
                    )
                    | models.Q(
                        state="rejected",
                        terminal_at__isnull=False,
                        result__isnull=True,
                        error__isnull=False,
                        payload_ciphertext__isnull=True,
                    )
                ),
                name="expiry_command_terminal_consistent",
            ),
        ]
        indexes = [
            models.Index(
                fields=(
                    "user_id",
                    "principal_incarnation",
                    "-recorded_at",
                    "command_id",
                ),
                name="expiry_command_journal_idx",
            ),
            models.Index(fields=("terminal_at",), name="expiry_command_retention_idx"),
        ]


class ExpiryCorrectionAudit(models.Model):
    command_id = models.UUIDField(unique=True)
    user_id = models.BigIntegerField()
    principal_incarnation = models.UUIDField()
    origin_session_id = models.UUIDField()
    patient_id = models.BigIntegerField()
    prescription_id = models.BigIntegerField()
    recorded_at = models.DateTimeField(default=timezone.now)
    before_revision = models.UUIDField()
    after_revision = models.UUIDField()
    before_start_date = models.DateField(null=True)
    before_end_date = models.DateField(null=True)
    after_start_date = models.DateField(null=True)
    after_end_date = models.DateField(null=True)


class ExpiryFutureAuthority(models.Model):
    prescription_id = models.BigIntegerField(primary_key=True)
    revision = models.UUIDField()
    generation = models.BigIntegerField(default=0)
