import contextlib
from datetime import datetime, timedelta

from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import BaseUserManager
from django.db import models

from payment.models import Subscription


def make_street_field():
    return models.CharField(max_length=30, blank=True, default="")


def make_zip_code_field():
    return models.CharField(max_length=5, blank=True, default="")


def make_city_field():
    return models.CharField(max_length=300, blank=True, default="")


def make_phone_field():
    return models.CharField(max_length=30, blank=True, default="")


class Patient(models.Model):
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

    def __str__(self):
        return str(self.firstname)


class NurseManager(BaseUserManager):
    def create_user(self, email=None, password=None, **extra_fields):
        if not email:
            raise ValueError("The Email field must be set")

        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save()
        return user

    def create_superuser(self, email=None, password=None, **extra_fields):
        user = self.create_user(email, password, **extra_fields)
        user.is_staff = True
        user.save()
        return user


class Nurse(AbstractBaseUser):
    username = models.CharField(max_length=30, blank=False)
    email = models.EmailField(unique=True, max_length=255, blank=False)
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    phone = make_phone_field()
    address = make_street_field()
    zip_code = make_zip_code_field()
    city = make_city_field()
    patients = models.ManyToManyField(Patient, blank=True)

    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    def has_perm(self, perm, obj=None):
        return True

    def has_module_perms(self, app_label):
        return True

    USERNAME_FIELD = "email"
    objects = NurseManager()

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
    user = models.OneToOneField(Nurse, on_delete=models.CASCADE)
    subscription_id = models.CharField(max_length=255, blank=True, null=True)
