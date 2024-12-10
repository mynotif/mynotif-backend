from unittest import mock
from unittest.mock import MagicMock, patch

import pytest
import stripe
from django.urls.base import reverse_lazy
from rest_framework import status

from payment.models import StripeProduct, Subscription


@pytest.fixture
def essentiel_product():
    return StripeProduct.objects.create(
        name="Essentiel",
        monthly_price_id="price_1QKSM0Klp91vafdhfkU8Ktz6",
        annual_price_id="price_1QKor4Klp91vdnl3Nw9eOJLg",
    )


@pytest.fixture
def client(authenticated_client):
    return authenticated_client


@pytest.mark.django_db
class TestSubscriptionViewSet:
    create_url = reverse_lazy("v1:payment:subscription-list")
    retrieve_url = reverse_lazy("v1:payment:subscription-detail", kwargs={"pk": 1})

    def test_endpoint(self):
        assert self.create_url == "/api/v1/payment/subscription/"
        assert self.retrieve_url == "/api/v1/payment/subscription/1/"

    @patch("payment.views.stripe.checkout.Session.create")
    def test_create_subscription_success(
        self, mock_create_session, client, user, essentiel_product
    ):
        client.force_login(user)
        data = {"plan": "monthly"}
        mock_checkout_session = MagicMock()
        mock_checkout_session.id = "session_id_example"
        mock_checkout_session.url = "https://checkout.stripe.com/pay/example"
        mock_create_session.return_value = mock_checkout_session
        response = client.post(self.create_url, data, format="json")
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["sessionId"] == "session_id_example"
        assert (
            response.data["checkout_url"] == "https://checkout.stripe.com/pay/example"
        )

    @patch("payment.views.stripe.checkout.Session.create")
    def test_create_subscription_product_not_found(
        self, mock_get_product, client, user
    ):
        client.force_login(user)
        data = {"plan": "monthly"}
        mock_get_product.side_effect = StripeProduct.DoesNotExist
        response = client.post(self.create_url, data, format="json")
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.data == {"detail": "No StripeProduct matches the given query."}

    @patch("payment.views.stripe.checkout.Session.create")
    def test_create_subscription_stripe_session_creation_failed(
        self, mock_create_session, client, user, essentiel_product
    ):
        client.force_login(user)
        data = {"plan": "monthly"}
        mock_create_session.side_effect = Exception("Error during session creation")
        response = client.post(self.create_url, data, format="json")
        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert {"error": "Error during session creation"} == response.data

    @patch("payment.views.stripe.checkout.Session.create")
    def test_retrieve_subscription_success(
        self, mock_create_session, client, user, essentiel_product
    ):
        client.force_login(user)
        Subscription.objects.create(
            user=user, stripe_subscription_id="session_id_example"
        )
        response = client.get(self.retrieve_url, format="json")
        assert response.status_code == status.HTTP_200_OK
        assert response.data["user"] == user.id
        assert response.data["stripe_subscription_id"] == "session_id_example"

    def test_retrieve_subscription_not_found(self, client, user):
        client.force_login(user)
        response = client.get(
            reverse_lazy("v1:payment:subscription-detail", kwargs={"pk": 9999}),
            format="json",
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.data == {"detail": "No Subscription matches the given query."}


@pytest.mark.django_db
class TestSubscriptionUserCancelView:
    cancel_user_url = reverse_lazy("v1:payment:subscription-user-cancel")

    def test_endpoint(self):
        assert self.cancel_user_url == "/api/v1/payment/subscriptions/user/cancel/"

    def test_subscription_user_cancel_view(self, client, user):
        client.force_login(user)

        subscription = Subscription.objects.create(
            user=user,
            stripe_subscription_id="sub_1FgsVx2R1LZ5sbG0fFqkg9Jz",
            cancel_at_period_end=False,
        )

        with mock.patch("stripe.Subscription.modify") as mock_stripe_modify:
            mock_stripe_modify.return_value = {
                "id": "sub_1FgsVx2R1LZ5sbG0fFqkg9Jz",
                "cancel_at_period_end": True,
            }
            response = client.post(self.cancel_user_url, data={}, format="json")

        assert response.status_code == status.HTTP_200_OK
        assert response.data == {"message": "Subscription cancelled successfully"}

        subscription.refresh_from_db()
        assert subscription.cancel_at_period_end is True
        assert subscription.active is False

    def test_subscription_user_cancel_stripe_error(self, client, user):
        client.force_login(user)

        subscription = Subscription.objects.create(
            user=user,
            stripe_subscription_id="sub_1FgsVx2R1LZ5sbG0fFqkg9Jz",
            cancel_at_period_end=False,
        )

        with mock.patch("stripe.Subscription.modify") as mock_stripe_modify:
            mock_stripe_modify.side_effect = stripe.error.InvalidRequestError(
                "Invalid subscription ID", "param"
            )
            response = client.post(self.cancel_user_url, data={}, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data == {"error": "Stripe error: Invalid subscription ID"}

        subscription.refresh_from_db()
        assert subscription.cancel_at_period_end is False


@pytest.mark.django_db
class TestSubscriptionSuccessView:
    success_url = reverse_lazy("v1:payment:subscription-success")

    def test_endpoint(self):
        assert self.success_url == "/api/v1/payment/subscriptions/success/"

    def test_subscription_success_view(self, client):
        response = client.get(self.success_url)
        assert response.status_code == status.HTTP_200_OK
        assert response.data == {"message": "Subscription successful"}


@pytest.mark.django_db
class TestSubscriptionCancelView:
    cancel_url = reverse_lazy("v1:payment:subscription-cancel")

    def test_endpoint(self):
        assert self.cancel_url == "/api/v1/payment/subscriptions/cancel/"

    def test_subscription_cancel_view(self, client):
        response = client.get(self.cancel_url)
        assert response.status_code == status.HTTP_200_OK
        assert response.data == {"message": "Subscription cancelled"}
