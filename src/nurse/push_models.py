"""Additive B07 registry; no clinical events or provider submission queue."""

import uuid

from django.conf import settings
from django.db import models
from django.db.models.signals import pre_delete
from django.dispatch import receiver


class PushRegistryState(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    restore_epoch = models.UUIDField(default=uuid.uuid4)
    accepting = models.BooleanField(default=False)


class PushDestination(models.Model):
    """A destination lock, never proof of a physical device or of ownership."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    namespace = models.CharField(max_length=120)
    lookup = models.CharField(max_length=64)
    epoch = models.PositiveBigIntegerField(default=0)
    generation = models.PositiveBigIntegerField(default=0)
    retain_until = models.DateTimeField(db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("namespace", "lookup"), name="push_destination_unique"
            )
        ]


class PushBinding(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    destination = models.ForeignKey(
        PushDestination, on_delete=models.CASCADE, related_name="bindings"
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="push_bindings",
    )
    session = models.ForeignKey(
        "main.AuthSession",
        null=True,
        on_delete=models.SET_NULL,
        related_name="push_bindings",
    )
    restore_epoch = models.UUIDField()
    epoch = models.PositiveBigIntegerField()
    generation = models.PositiveBigIntegerField()
    public_key_digest = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)
    revoked_at = models.DateTimeField(null=True, db_index=True)
    barrier_confirmed = models.BooleanField(default=False)
    in_flight = models.CharField(max_length=20, default="unknown_or_present")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("destination",),
                condition=models.Q(revoked_at__isnull=True),
                name="push_one_active_destination",
            ),
            models.UniqueConstraint(
                fields=("destination", "generation"), name="push_binding_generation"
            ),
            models.CheckConstraint(
                condition=models.Q(expires_at__gt=models.F("created_at")),
                name="push_binding_expiry",
            ),
        ]


class PushTokenVersion(models.Model):
    binding = models.OneToOneField(
        PushBinding,
        primary_key=True,
        on_delete=models.CASCADE,
        related_name="token_version",
    )
    envelope = models.JSONField()


class PushEnrollment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="push_enrollments",
    )
    session = models.ForeignKey(
        "main.AuthSession", on_delete=models.CASCADE, related_name="push_enrollments"
    )
    destination = models.ForeignKey(
        PushDestination, on_delete=models.CASCADE, related_name="enrollments"
    )
    request_id = models.UUIDField()
    body_digest = models.CharField(max_length=64)
    restore_epoch = models.UUIDField()
    binding_id = models.UUIDField(default=uuid.uuid4)
    previous_binding_id = models.UUIDField(null=True)
    previous_epoch = models.PositiveBigIntegerField(null=True)
    previous_generation = models.PositiveBigIntegerField(null=True)
    expected_epoch = models.PositiveBigIntegerField()
    expected_generation = models.PositiveBigIntegerField()
    context = models.JSONField(default=dict)
    public_key = models.CharField(max_length=87, blank=True)
    commitment = models.CharField(max_length=64, blank=True)
    envelope = models.JSONField(default=dict)
    status = models.CharField(max_length=16, default="pending")
    failures = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)
    nonce_expires_at = models.DateTimeField()
    result_until = models.DateTimeField(db_index=True)
    result = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("session", "request_id"), name="push_session_request"
            ),
            models.UniqueConstraint(
                fields=("session", "destination"),
                condition=models.Q(status="pending"),
                name="push_one_pending_session_dest",
            ),
        ]


class PushQuota(models.Model):
    key = models.CharField(primary_key=True, max_length=64)
    events = models.JSONField(default=list)
    expires_at = models.DateTimeField(db_index=True)


@receiver(pre_delete, sender=settings.AUTH_USER_MODEL, dispatch_uid="b07_user_erasure")
def erase_user_push(sender, instance, **kwargs):
    from nurse.push_services import invalidate_account

    invalidate_account(instance.pk, erase=True)


@receiver(pre_delete, sender="main.AuthSession", dispatch_uid="b07_session_erasure")
def erase_session_push(sender, instance, **kwargs):
    from nurse.push_services import invalidate_session

    invalidate_session(instance.pk)
