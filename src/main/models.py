import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models


class AuthSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="auth_sessions"
    )
    device_label = models.CharField(max_length=80, blank=True, default="")
    created_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    last_seen_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    password_fingerprint = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(expires_at__gt=models.F("created_at")),
                name="as_exp_after_create",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    expires_at__lte=models.F("created_at") + timedelta(days=30)
                ),
                name="as_max_age_30d",
            ),
            models.CheckConstraint(
                condition=models.Q(revoked_at__isnull=True)
                | models.Q(revoked_at__gte=models.F("created_at")),
                name="as_revoke_after_create",
            ),
            models.CheckConstraint(
                condition=models.Q(last_seen_at__isnull=True)
                | models.Q(last_seen_at__gte=models.F("created_at")),
                name="as_seen_after_create",
            ),
        ]
        indexes = [
            models.Index(fields=("user", "revoked_at"), name="as_user_revoked"),
            models.Index(fields=("expires_at",), name="as_expires"),
        ]


class AuthToken(models.Model):
    class Kind(models.TextChoices):
        ACCESS = "access", "Access"
        REFRESH = "refresh", "Refresh"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        AuthSession, on_delete=models.CASCADE, related_name="tokens"
    )
    kind = models.CharField(max_length=7, choices=Kind.choices)
    generation = models.PositiveIntegerField()
    token_hash = models.CharField(max_length=64, unique=True)
    issued_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    rotation_request_hash = models.CharField(max_length=64, null=True, blank=True)
    recovery_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("session", "kind", "generation"),
                name="at_session_kind_generation",
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=("access", "refresh")), name="at_kind_valid"
            ),
            models.CheckConstraint(
                condition=models.Q(expires_at__gt=models.F("issued_at")),
                name="at_exp_after_issue",
            ),
            models.CheckConstraint(
                condition=models.Q(consumed_at__isnull=True)
                | models.Q(consumed_at__gte=models.F("issued_at")),
                name="at_consumed_after_issue",
            ),
            models.CheckConstraint(
                condition=models.Q(revoked_at__isnull=True)
                | models.Q(revoked_at__gte=models.F("issued_at")),
                name="at_revoked_after_issue",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        consumed_at__isnull=True,
                        rotation_request_hash__isnull=True,
                        recovery_until__isnull=True,
                    )
                    | models.Q(
                        kind="refresh",
                        consumed_at__isnull=False,
                        rotation_request_hash__isnull=False,
                        recovery_until__isnull=False,
                    )
                ),
                name="at_rotation_fields",
            ),
            models.CheckConstraint(
                condition=models.Q(recovery_until__isnull=True)
                | (
                    models.Q(recovery_until__gt=models.F("consumed_at"))
                    & models.Q(
                        recovery_until__lte=models.F("consumed_at")
                        + timedelta(seconds=60)
                    )
                    & models.Q(recovery_until__lte=models.F("expires_at"))
                ),
                name="at_recovery_bound",
            ),
        ]
        indexes = [models.Index(fields=("expires_at",), name="at_expires")]
