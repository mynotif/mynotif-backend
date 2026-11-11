"""Durable C12 admissions, independent of clinical and transport retention."""

import uuid

from django.db import models
from django.utils import timezone

from nurse.expiry_calculation import CALCULATION_VERSION


class ExpiryReminderAuthority(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    namespace = models.UUIDField(default=uuid.uuid4)
    accepting = models.BooleanField(default=False)


class ExpiryReminderPreference(models.Model):
    user_id = models.BigIntegerField(primary_key=True)
    principal_incarnation = models.UUIDField()
    lead_days = models.PositiveSmallIntegerField(default=10)
    revision = models.UUIDField(default=uuid.uuid4)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(lead_days__in=(7, 8, 9, 10)),
                name="reminder_lead_days_valid",
            )
        ]


class ExpiryReminderOpportunity(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id = models.BigIntegerField()
    principal_incarnation = models.UUIDField()
    prescription_id = models.BigIntegerField(db_index=True)
    end_date = models.DateField()
    kind = models.CharField(max_length=16)
    due_date = models.DateField()
    eligible_date = models.DateField()
    future_revision = models.UUIDField()
    future_generation = models.BigIntegerField()
    preference_revision = models.UUIDField()
    calculation_version = models.CharField(max_length=64, default=CALCULATION_VERSION)
    state = models.CharField(max_length=16, default="planned")
    admitted_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("user_id", "prescription_id", "end_date", "kind"),
                name="reminder_opportunity_identity",
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=("prealert", "j1")),
                name="reminder_kind_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    state__in=("planned", "admitted", "missed", "cancelled")
                ),
                name="reminder_opportunity_state",
            ),
        ]


class ExpiryReminderEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id = models.BigIntegerField()
    principal_incarnation = models.UUIDField()
    namespace = models.UUIDField()
    service_day = models.DateField()
    deadline_at = models.DateTimeField()
    admitted_at = models.DateTimeField(default=timezone.now)
    state = models.CharField(max_length=16, default="reserved")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("user_id", "service_day"), name="reminder_daily_identity"
            ),
            models.CheckConstraint(
                condition=models.Q(state__in=("reserved", "cancelled")),
                name="reminder_event_state",
            ),
        ]


class ExpiryReminderEventItem(models.Model):
    event = models.ForeignKey(
        ExpiryReminderEvent, on_delete=models.DO_NOTHING, db_constraint=False
    )
    opportunity = models.OneToOneField(
        ExpiryReminderOpportunity, on_delete=models.DO_NOTHING, db_constraint=False
    )


class ExpiryReminderEventRoute(models.Model):
    event = models.ForeignKey(
        ExpiryReminderEvent, on_delete=models.DO_NOTHING, db_constraint=False
    )
    device_ref = models.CharField(max_length=255)
    namespace = models.CharField(max_length=120)
    binding_id = models.UUIDField(db_index=True)
    epoch = models.PositiveBigIntegerField()
    generation = models.PositiveBigIntegerField()
    restore_epoch = models.UUIDField()
    state = models.CharField(max_length=16, default="reserved")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("event", "device_ref"), name="reminder_event_device"
            ),
            models.UniqueConstraint(
                fields=("event", "binding_id"), name="reminder_event_binding"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    state__in=("reserved", "refused", "uncertain", "cancelled")
                ),
                name="reminder_route_state",
            ),
        ]


class ExpiryReminderTransition(models.Model):
    opportunity_id = models.UUIDField()
    from_state = models.CharField(max_length=16)
    to_state = models.CharField(max_length=16)
    recorded_at = models.DateTimeField(default=timezone.now)
