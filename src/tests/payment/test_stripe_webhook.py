import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.test import Client
from django.urls import reverse_lazy
from django.utils import timezone
from rest_framework import status

from payment.models import CustomerDetail, Subscription

STRIPE_WEBHOOK_SECRET = "whsec_testsecret"


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def customer_subscription_deleted_payload():
    return {
        "type": "customer.subscription.deleted",
        "data": {
            "object": {
                "customer": "cus_REbNQXKKFCRF2c",
            }
        },
    }


@pytest.fixture
def invoice_paid_payload():
    return {
        "type": "invoice.paid",
        "data": {
            "object": {
                "customer": "cus_REbNQXKKFCRF2c",
                "hosted_invoice_url": "https://stripe.com/invoice/test123",
                "invoice_pdf": "https://stripe.com/invoice/test123.pdf",
            }
        },
    }


@pytest.fixture
def customer_subscription_updated_payload():
    return {
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "current_period_end": 1734440765,  # Timestamp for period end
                "current_period_start": 1731848765,  # Timestamp for period start
                "customer": "cus_REbNQXKKFCRF2c",
                "cancel_at_period_end": False,
                "trial_end": "null",  # No trial
                "items": {
                    "data": [
                        {
                            "plan": {
                                "active": True,
                            },
                        },
                    ],
                },
            }
        },
    }


@pytest.fixture
def checkout_session_completed_payload():
    """Fixture to generate a 'checkout.session.completed' event payload."""
    return {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_a1SdT2v81sae3cW",
                "object": "checkout.session",
                "amount_subtotal": 990,
                "amount_total": 990,
                "billing_address_collection": "required",
                "cancel_url": "http://127.0.0.1:8000/api/v1/subscriptions/cancel/",
                "created": 1731848739,
                "currency": "eur",
                "custom_fields": [],
                "customer": "cus_REbNQXKKFCRF2c",
                "customer_creation": "always",
                "customer_details": {
                    "address": {
                        "city": "Saint-Ouen-l'Aumône",
                        "country": "FR",
                        "line1": "1 Rue Pagnere",
                        "line2": "null",
                        "postal_code": "95310",
                        "state": "null",
                    },
                    "email": "kadi@test.com",
                    "name": "kadi",
                },
                "customer_email": "kadi@test.com",
                "expires_at": 1731935139,
                "metadata": {"product_name": "Essentiel", "user_id": "5"},
                "mode": "subscription",
                "payment_status": "paid",
                "status": "complete",
                "subscription": "sub_1QTmqQKlp91vayS3kwoCFUvT",
            },
        },
    }


def generate_stripe_signature(payload, secret):
    """
    Simulates the Stripe signature for the test using the secret key.
    """
    payload_str = json.dumps(payload)
    timestamp = str(int(time.time()))
    sig_base = f"{timestamp}.{payload_str}"
    signature = hmac.new(
        secret.encode("utf-8"), sig_base.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={signature}"


@pytest.mark.django_db
class TestStripeWebhook:
    url = reverse_lazy("v1:payment:stripe-webhook")

    def test_endpoint(self):
        assert self.url == "/api/v1/payment/stripe/webhook/"

    @mock.patch("stripe.Webhook.construct_event")
    def test_checkout_session_completed_with_valid_signature(
        self, mock_construct_event, client, checkout_session_completed_payload, user
    ):
        checkout_session_completed_payload["data"]["object"]["metadata"]["user_id"] = (
            str(user.id)
        )

        sig_header = generate_stripe_signature(
            checkout_session_completed_payload, STRIPE_WEBHOOK_SECRET
        )

        mock_construct_event = mock.Mock(
            return_value=checkout_session_completed_payload
        )
        with mock.patch("stripe.Webhook.construct_event", mock_construct_event):
            response = client.post(
                self.url,
                data=json.dumps(checkout_session_completed_payload),
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE=sig_header,
            )
        assert response.status_code == status.HTTP_200_OK

    @mock.patch("stripe.Webhook.construct_event")
    def test_checkout_session_completed_with_invalid_signature(
        self, mock_construct_event, client, checkout_session_completed_payload
    ):
        mock_construct_event.side_effect = ValueError("Invalid signature")
        invalid_sig_header = "invalid_signature"
        response = client.post(
            self.url,
            data=checkout_session_completed_payload,
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE=invalid_sig_header,
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    @mock.patch("stripe.Webhook.construct_event")
    def test_checkout_session_completed(
        self, mock_construct_event, client, user, checkout_session_completed_payload
    ):
        """
        Complete test of the `checkout.session.completed`
        function.
        Checks that `CustomerDetail` and `Subscription`
        are correctly created or updated.
        """
        sig_header = generate_stripe_signature(
            checkout_session_completed_payload, STRIPE_WEBHOOK_SECRET
        )

        checkout_session_completed_payload["data"]["object"]["metadata"][
            "user_id"
        ] = user.id
        checkout_session_completed_payload["data"]["object"]["id"] = "sub_test_id"

        mock_construct_event = mock.Mock(
            return_value=checkout_session_completed_payload
        )
        with mock.patch("stripe.Webhook.construct_event", mock_construct_event):
            response = client.post(
                self.url,
                data=json.dumps(checkout_session_completed_payload),
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE=sig_header,
            )

        assert response.status_code == status.HTTP_200_OK

        customer_detail = CustomerDetail.objects.get(user=user)
        assert (
            customer_detail.stripe_customer_id
            == checkout_session_completed_payload["data"]["object"]["customer"]
        )
        assert (
            customer_detail.city
            == checkout_session_completed_payload["data"]["object"]["customer_details"][
                "address"
            ]["city"]
        )
        assert (
            customer_detail.country
            == checkout_session_completed_payload["data"]["object"]["customer_details"][
                "address"
            ]["country"]
        )
        assert (
            customer_detail.address
            == checkout_session_completed_payload["data"]["object"]["customer_details"][
                "address"
            ]["line1"]
        )
        assert (
            customer_detail.postal_code
            == checkout_session_completed_payload["data"]["object"]["customer_details"][
                "address"
            ]["postal_code"]
        )
        assert (
            customer_detail.email
            == checkout_session_completed_payload["data"]["object"]["customer_details"][
                "email"
            ]
        )

        subscription = Subscription.objects.get(user=user)
        assert (
            subscription.stripe_subscription_id
            == checkout_session_completed_payload["data"]["object"]["subscription"]
        )
        assert (
            subscription.status
            == checkout_session_completed_payload["data"]["object"]["status"]
        )
        assert (
            subscription.payment_status
            == checkout_session_completed_payload["data"]["object"]["payment_status"]
        )
        assert (
            subscription.product_name
            == checkout_session_completed_payload["data"]["object"]["metadata"][
                "product_name"
            ]
        )
        assert subscription.total_price == checkout_session_completed_payload["data"][
            "object"
        ]["amount_total"] / Decimal(100)

    @mock.patch("stripe.Webhook.construct_event")
    def test_checkout_session_completed_user_does_not_exist(
        self, mock_construct_event, client, checkout_session_completed_payload
    ):

        checkout_session_completed_payload["data"]["object"]["metadata"][
            "user_id"
        ] = "9999"

        sig_header = generate_stripe_signature(
            checkout_session_completed_payload, STRIPE_WEBHOOK_SECRET
        )

        mock_construct_event = mock.Mock(
            return_value=checkout_session_completed_payload
        )

        with mock.patch("stripe.Webhook.construct_event", mock_construct_event):
            response = client.post(
                self.url,
                data=json.dumps(checkout_session_completed_payload),
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE=sig_header,
            )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        # assert response.content == b"User does not exist" ToDo

    @mock.patch("stripe.Webhook.construct_event")
    def test_customer_subscription_updated(
        self, mock_construct_event, customer_subscription_updated_payload, client, user
    ):
        """
        Complete test of the `customer.subscription.updated` function.
        Checks that `Subscription` is correctly updated.
        """

        # Create a CustomerDetail object to simulate the Stripe user
        CustomerDetail.objects.create(
            user=user,
            stripe_customer_id=customer_subscription_updated_payload["data"]["object"][
                "customer"
            ],
        )

        # Create an initial subscription (before the update)
        Subscription.objects.create(
            user=user,
            cancel_at_period_end=False,
            current_period_start=timezone.now(),
            current_period_end=timezone.now() + timedelta(days=30),
            active=True,
        )

        # Generate the signature header for Stripe (adapt according to your logic)
        sig_header = generate_stripe_signature(
            customer_subscription_updated_payload, STRIPE_WEBHOOK_SECRET
        )

        mock_construct_event = mock.Mock(
            return_value=customer_subscription_updated_payload
        )

        # API webhook call
        with mock.patch("stripe.Webhook.construct_event", mock_construct_event):
            response = client.post(
                self.url,  # Ensure the URL is correct
                data=json.dumps(customer_subscription_updated_payload),
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE=sig_header,  # Header signature
            )

        # Verify the response is correct
        assert response.status_code == status.HTTP_200_OK

        customer = CustomerDetail.objects.get(
            stripe_customer_id=customer_subscription_updated_payload["data"]["object"][
                "customer"
            ]
        )
        user = customer.user

        # Retrieve the subscription after the update
        subscription = Subscription.objects.get(user=user)

        # Check that the subscription values are updated as in the Stripe event
        assert (
            subscription.cancel_at_period_end
            == customer_subscription_updated_payload["data"]["object"][
                "cancel_at_period_end"
            ]
        )
        assert subscription.trial_end == (
            None
            if customer_subscription_updated_payload["data"]["object"]["trial_end"]
            == "null"
            else timezone.make_aware(
                datetime.fromtimestamp(
                    customer_subscription_updated_payload["data"]["object"]["trial_end"]
                )
            )
        )
        assert subscription.active == bool(
            customer_subscription_updated_payload["data"]["object"]["items"]["data"][0][
                "plan"
            ]["active"]
        )

    @mock.patch("stripe.Webhook.construct_event")
    def test_invoice_paid(
        self, mock_construct_event, invoice_paid_payload, client, user
    ):
        """
        Test the `invoice.paid` event.
        Verify that the `hosted_invoice_url` and `invoice_pdf` fields
        of the subscription are correctly updated.
        """

        CustomerDetail.objects.create(
            user=user,
            stripe_customer_id=invoice_paid_payload["data"]["object"]["customer"],
        )

        Subscription.objects.create(
            user=user, status="active", hosted_invoice_url="", invoice_pdf=""
        )

        sig_header = generate_stripe_signature(
            invoice_paid_payload, STRIPE_WEBHOOK_SECRET
        )

        mock_construct_event = mock.Mock(return_value=invoice_paid_payload)

        with mock.patch("stripe.Webhook.construct_event", mock_construct_event):
            response = client.post(
                self.url,
                data=json.dumps(invoice_paid_payload),
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE=sig_header,
            )

        assert response.status_code == status.HTTP_200_OK

        subscription = Subscription.objects.get(user=user)
        assert (
            subscription.hosted_invoice_url
            == invoice_paid_payload["data"]["object"]["hosted_invoice_url"]
        )
        assert (
            subscription.invoice_pdf
            == invoice_paid_payload["data"]["object"]["invoice_pdf"]
        )

    @mock.patch("stripe.Webhook.construct_event")
    def test_customer_subscription_deleted(
        self, mock_construct_event, customer_subscription_deleted_payload, client, user
    ):
        """
        Test the `customer.subscription.deleted` event.
        Verify that the subscription status is correctly updated to `Canceled`.
        """

        CustomerDetail.objects.create(
            user=user,
            stripe_customer_id=customer_subscription_deleted_payload["data"]["object"][
                "customer"
            ],
        )

        Subscription.objects.create(user=user, status="active")

        sig_header = generate_stripe_signature(
            customer_subscription_deleted_payload, STRIPE_WEBHOOK_SECRET
        )

        mock_construct_event = mock.Mock(
            return_value=customer_subscription_deleted_payload
        )

        with mock.patch("stripe.Webhook.construct_event", mock_construct_event):
            response = client.post(
                self.url,
                data=json.dumps(customer_subscription_deleted_payload),
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE=sig_header,
            )

        assert response.status_code == status.HTTP_200_OK

        subscription = Subscription.objects.get(user=user)
        assert subscription.status == "Cancelled"
