import contextlib
import uuid
from datetime import datetime, timedelta

from django.contrib.auth.models import User
from django.db import models

from nurse.patient_search import PatientQuerySet, PatientSearchSaveMixin
from payment.models import Subscription


def make_street_field():
    return models.CharField(max_length=30, blank=True, default="")


def make_zip_code_field():
    return models.CharField(max_length=5, blank=True, default="")


def make_city_field():
    return models.CharField(max_length=300, blank=True, default="")


def make_phone_field():
    return models.CharField(max_length=30, blank=True, default="")


class Patient(PatientSearchSaveMixin, models.Model):
    firstname = models.CharField(max_length=30, blank=False)
    lastname = models.CharField(max_length=30, blank=False)
    street = make_street_field()
    zip_code = make_zip_code_field()
    city = make_city_field()
    phone = make_phone_field()
    birthday = models.DateField(
        auto_now=False, auto_now_add=False, blank=True, null=True
    )
    health_card_number = models.CharField(
        max_length=15, blank=True, help_text="carte vitale"
    )
    ss_provider_code = models.CharField(
        max_length=9, blank=True, help_text="code d'organisme de rattachement"
    )

    # Nullable during additive rollout; activate v2 only after repair/check.
    search_firstname = models.TextField(null=True, editable=False)
    search_lastname = models.TextField(null=True, editable=False)
    search_city = models.TextField(null=True, editable=False)
    search_phone = models.TextField(null=True, editable=False)
    objects = PatientQuerySet.as_manager()

    def __str__(self):
        return str(self.firstname)


class Nurse(models.Model):
    user = models.OneToOneField(User, on_delete=models.SET_NULL, null=True)
    phone = make_phone_field()
    address = make_street_field()
    zip_code = make_zip_code_field()
    city = make_city_field()
    patients = models.ManyToManyField(Patient, blank=True)

    def __str__(self):
        return str(self.user)

    def get_active_subscription(self):
        """Returns the active subscription"""
        """for the nurse's user if it exists, otherwise None."""
        with contextlib.suppress(Subscription.DoesNotExist):
            return Subscription.objects.get(user=self.user, active=True)

    def has_active_subscription(self):
        """Returns True if the nurse has an active subscription."""
        return self.get_active_subscription() is not None


class PrescriptionManager(models.Manager):
    DEFAULT_EXPIRING_SOON_DAYS = 7

    def expiring_soon(self, days=DEFAULT_EXPIRING_SOON_DAYS):
        today = datetime.now().date()
        expiring_soon_date = today + timedelta(days=days)
        return self.filter(end_date__lte=expiring_soon_date, end_date__gte=today)


class Prescription(models.Model):
    prescribing_doctor = models.CharField(max_length=300, blank=False)
    email_doctor = models.EmailField(blank=True, null=True)
    start_date = models.DateField(
        auto_now=False, auto_now_add=False, blank=True, null=True
    )
    end_date = models.DateField(
        auto_now=False, auto_now_add=False, blank=True, null=True
    )
    photo_prescription = models.ImageField(upload_to="prescriptions")
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, null=False)
    objects = PrescriptionManager()

    def __str__(self):
        return f"Prescription: {self.prescribing_doctor}"

    def is_valid(self):
        """Returns true if the prescription is still valid (e.g. hasn't expired)."""
        return bool(
            self.start_date
            and self.end_date
            and self.start_date <= datetime.now().date() <= self.end_date
        )

    def expiring_soon(self, days=PrescriptionManager.DEFAULT_EXPIRING_SOON_DAYS):
        """Returns true if the prescription is expiring soon."""
        if self.end_date is None:
            return False
        today = datetime.now().date()
        expiring_soon_date = today + timedelta(days=days)
        return self.end_date <= expiring_soon_date and self.end_date >= today


class DocumentImportState(models.TextChoices):
    RECEIVING = "receiving"
    VALIDATING = "validating"
    READY = "ready"
    RETRYABLE = "retryable"
    REJECTED = "rejected"
    CONFIRMED = "confirmed"
    ABANDONED = "abandoned"
    EXPIRED = "expired"


class DocumentImportAttempt(models.Model):
    """One authenticated import attempt; retained briefly to rate-limit retries."""

    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="document_import_attempts",
        db_index=False,
    )
    attempted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(
                fields=("owner", "attempted_at"),
                name="nurse_doc_attempt_owner_idx",
            ),
        ]


class DocumentImport(models.Model):
    """Server-owned, recoverable staging operation for a prescription document."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="prescription_document_imports"
    )
    patient = models.ForeignKey(
        Patient, on_delete=models.PROTECT, related_name="document_imports"
    )
    import_uuid = models.UUIDField()
    state = models.CharField(
        max_length=16,
        choices=DocumentImportState.choices,
        default=DocumentImportState.RECEIVING,
    )
    revision = models.PositiveIntegerField(default=1)
    metadata = models.JSONField(null=True, blank=True)
    initial_fingerprint = models.CharField(max_length=64, null=True, blank=True)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    prescription = models.ForeignKey(
        Prescription,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="document_imports",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("owner", "import_uuid"),
                name="nurse_doc_import_owner_uuid_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=("state", "expires_at"), name="nurse_docimp_exp_idx"),
            models.Index(
                fields=("owner", "patient", "created_at"),
                name="nurse_docimp_scope_idx",
            ),
        ]


class PrescriptionDocumentVersion(models.Model):
    """Immutable metadata snapshot associated with one confirmed prescription."""

    prescription = models.ForeignKey(
        Prescription, on_delete=models.PROTECT, related_name="document_versions"
    )
    document_import = models.OneToOneField(
        DocumentImport, on_delete=models.PROTECT, related_name="document_version"
    )
    version = models.PositiveIntegerField()
    prescribing_doctor = models.CharField(max_length=300, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("prescription", "version"),
                name="nurse_doc_version_prescription_uniq",
            ),
        ]


class PrescriptionDocumentAsset(models.Model):
    """Opaque reference to immutable bytes in private storage."""

    class Role(models.TextChoices):
        ORIGINAL = "original"
        DERIVED = "derived"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document_import = models.ForeignKey(
        DocumentImport, on_delete=models.PROTECT, related_name="assets"
    )
    document_version = models.ForeignKey(
        PrescriptionDocumentVersion,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="assets",
    )
    role = models.CharField(max_length=8, choices=Role.choices)
    original_index = models.PositiveSmallIntegerField(null=True, blank=True)
    source_asset = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="derived_assets",
    )
    storage_key = models.CharField(max_length=512, unique=True)
    mime_type = models.CharField(max_length=64)
    byte_size = models.PositiveBigIntegerField()
    page_count = models.PositiveSmallIntegerField()
    sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("document_import", "original_index"),
                condition=models.Q(role="original"),
                name="nurse_doc_asset_original_index_uniq",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        role="original",
                        source_asset__isnull=True,
                        original_index__isnull=False,
                    )
                    | models.Q(
                        role="derived",
                        source_asset__isnull=False,
                        original_index__isnull=True,
                    )
                ),
                name="nurse_doc_asset_source_role_check",
            ),
            models.CheckConstraint(
                condition=models.Q(byte_size__gt=0) & models.Q(page_count__gt=0),
                name="nurse_doc_asset_nonempty_check",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("Document asset metadata is immutable")
        return super().save(*args, **kwargs)


class UserOneSignalProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    subscription_id = models.CharField(max_length=255, blank=True, null=True)
