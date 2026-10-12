import contextlib
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
    start_date = models.DateField(auto_now=False, auto_now_add=False)
    end_date = models.DateField(auto_now=False, auto_now_add=False)
    photo_prescription = models.ImageField(upload_to="prescriptions")
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, null=False)
    objects = PrescriptionManager()

    def __str__(self):
        return f"Prescription: {self.prescribing_doctor}"

    def is_valid(self):
        """Returns true if the prescription is still valid (e.g. hasn't expired)."""
        return self.start_date <= datetime.now().date() <= self.end_date

    def expiring_soon(self, days=PrescriptionManager.DEFAULT_EXPIRING_SOON_DAYS):
        """Returns true if the prescription is expiring soon."""
        today = datetime.now().date()
        expiring_soon_date = today + timedelta(days=days)
        return self.end_date <= expiring_soon_date and self.end_date >= today


class UserOneSignalProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    subscription_id = models.CharField(max_length=255, blank=True, null=True)
