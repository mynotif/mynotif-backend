from django.db import models

from nurse.models import User


class Subscription(models.Model):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="subscription"
    )
    stripe_subscription_id = models.CharField(max_length=255, unique=True)
    status = models.CharField(max_length=50, null=True)
    payment_status = models.CharField(max_length=50, null=True)
    active = models.BooleanField(default=False)
    product_name = models.CharField(max_length=255, null=True, blank=True)
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    trial_end = models.DateTimeField(null=True, blank=True)
    total_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    hosted_invoice_url = models.URLField(max_length=500, null=True, blank=True)
    invoice_pdf = models.URLField(max_length=500, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Subscription of {self.user.username}"


class CustomerDetail(models.Model):
    """
    CustomerDetail model stores additional information
    about a user related to payment details.
    """

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="customer_details"
    )
    stripe_customer_id = models.CharField(
        max_length=255,
        unique=True,
        help_text="The unique identifier for the customer in Stripe",
    )
    city = models.CharField(max_length=100, null=True, blank=True)
    country = models.CharField(
        max_length=2, null=True, blank=True, help_text="ISO 3166-1 alpha-2 country code"
    )
    address = models.CharField(max_length=255, null=True, blank=True)
    postal_code = models.CharField(max_length=20, null=True, blank=True)
    email = models.EmailField(
        null=True,
        blank=True,
        help_text="The email address used for payment, "
        "which may be different from the registered email",
    )

    def __str__(self):
        return f"CustomerDetails for {self.user.username}"


class StripeProduct(models.Model):
    name = models.CharField(max_length=255)
    product_id = models.CharField(max_length=255)
    monthly_price_id = models.CharField(max_length=255)
    annual_price_id = models.CharField(max_length=255)

    def __str__(self):
        return f"StripeProduct for {self.name}"
