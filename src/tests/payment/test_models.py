from datetime import datetime, timedelta

import pytest
from django.contrib.auth.models import User

from payment.models import CustomerDetail, StripeProduct, Subscription


@pytest.fixture
def user():
    return User.objects.create_user(username="testuser", password="password")


@pytest.fixture
def subscription(user):
    return Subscription.objects.create(
        user=user,
        stripe_subscription_id="sub_123456789",
        status="active",
        payment_status="paid",
        active=True,
        product_name="Essentiel",
        current_period_start=datetime.now(),
        current_period_end=datetime.now() + timedelta(days=30),
        cancel_at_period_end=False,
        trial_end=None,
        total_price=10.0,
        hosted_invoice_url="https://hosted_invoice_url.com",
        invoice_pdf="https://invoice_pdf.com",
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )


@pytest.fixture
def stripe_product():
    return StripeProduct.objects.create(
        name="Essentiel",
        product_id="prod_RCs63heMwKdOGZ",
        monthly_price_id="price_1QKSM0Klp91vayS3fkU8Ktz6",
        annual_price_id="price_1QKor4Klp91vayS3Nw9eOJLg",
    )


@pytest.mark.django_db
class TestSubscription:
    def test_subscription_creation(self, subscription, user):
        assert subscription.user == user
        assert subscription.stripe_subscription_id == "sub_123456789"
        assert subscription.status == "active"
        assert subscription.payment_status == "paid"
        assert subscription.active is True
        assert subscription.product_name == "Essentiel"
        assert subscription.current_period_start.date() == datetime.now().date()
        assert (
            subscription.current_period_end.date()
            == (datetime.now() + timedelta(days=30)).date()
        )
        assert not subscription.cancel_at_period_end
        assert subscription.trial_end is None
        assert subscription.total_price == 10.0
        assert subscription.hosted_invoice_url == "https://hosted_invoice_url.com"
        assert subscription.invoice_pdf == "https://invoice_pdf.com"
        assert subscription.created_at.date() == datetime.now().date()
        assert subscription.updated_at.date() == datetime.now().date()

    def test_subscription_str(self, subscription):
        assert str(subscription) == f"Subscription of {subscription.user.username}"


@pytest.mark.django_db
class TestStripeProduct:
    def test_stripe_product_creation(self, stripe_product):
        assert stripe_product.name == "Essentiel"
        assert stripe_product.product_id == "prod_RCs63heMwKdOGZ"
        assert stripe_product.monthly_price_id == "price_1QKSM0Klp91vayS3fkU8Ktz6"
        assert stripe_product.annual_price_id == "price_1QKor4Klp91vayS3Nw9eOJLg"

    def test_stripe_product_str(self, stripe_product):
        assert str(stripe_product) == f"StripeProduct for {stripe_product.name}"


@pytest.mark.django_db
class TestCustomerDetail:
    def test_customer_detail_str(self, user):
        customer_detail = CustomerDetail.objects.create(
            user=user,
            stripe_customer_id="cus_TEST123",
            city="Paris",
            country="FR",
            address="123 Rue de Test",
            postal_code="75001",
            email="testuser@example.com",
        )
        assert str(customer_detail) == f"CustomerDetails for {user.username}"
