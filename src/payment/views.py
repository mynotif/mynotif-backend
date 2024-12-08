import stripe
from django.conf import settings
from django.shortcuts import get_object_or_404
from django.urls import reverse
from rest_framework import status, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from payment.models import StripeProduct, Subscription
from payment.serializers import SubscriptionSerializer

ESSENTIAL_PLAN_NAME = "Essentiel"


class SubscriptionViewSet(viewsets.ModelViewSet):
    stripe.api_key = settings.STRIPE_API_KEY
    stripe.api_version = settings.STRIPE_API_VERSION
    plan_name = ESSENTIAL_PLAN_NAME = "Essentiel"

    queryset = Subscription.objects.all()
    serializer_class = SubscriptionSerializer

    def get_queryset(self):
        return self.queryset.filter(user=self.request.user)

    def create(self, request, *args, **kwargs):
        user = request.user
        plan = request.data.get("plan")  # "monthly" ou "annual"

        try:
            product = StripeProduct.objects.get(name=self.plan_name)
            price_id = (
                product.monthly_price_id
                if plan == "monthly"
                else product.annual_price_id
            )
        except StripeProduct.DoesNotExist:
            return Response({"error": "Product not found"}, status=404)

        try:
            checkout_session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                customer_email=user.email,
                line_items=[{"price": price_id, "quantity": 1}],
                mode="subscription",
                billing_address_collection="required",
                currency="eur",
                success_url=request.build_absolute_uri(
                    reverse("v1:payment:subscription-success")
                ),
                cancel_url=request.build_absolute_uri(
                    reverse("v1:payment:subscription-cancel")
                ),
                metadata={
                    "user_id": user.id,
                    "product_name": product.name,
                },
            )

            data = {
                **request.data,
                "stripe_subscription_id": checkout_session.id,
            }

            headers = self.get_success_headers(data)
            return Response(
                {
                    "sessionId": checkout_session.id,
                    "checkout_url": checkout_session.url,
                },
                status=status.HTTP_201_CREATED,
                headers=headers,
            )
        except Exception as e:
            print("Error during session creation:", e)
            return Response({"error": str(e)}, status=500)

    def retrieve(self, request, *args, **kwargs):
        obj = get_object_or_404(self.get_queryset(), user=request.user)
        serializer = self.get_serializer(obj)
        return Response(serializer.data)


class SubscriptionUserCancelView(APIView):
    """
    Handles the cancellation of a subscription by the user.

    This endpoint is used to cancel a subscription by the user.
    It is called when the user requests to cancel their subscription.
    """

    def post(self, request, *args, **kwargs):
        user = request.user
        subscription = get_object_or_404(Subscription, user=user)
        stripe.api_key = settings.STRIPE_API_KEY

        try:
            stripe.Subscription.modify(
                subscription.stripe_subscription_id, cancel_at_period_end=True
            )

            subscription.cancel_at_period_end = True
            subscription.save()

            return Response(
                {"message": "Subscription cancelled successfully"},
                status=status.HTTP_200_OK,
            )
        except stripe.error.InvalidRequestError as e:
            return Response(
                {"error": f"Stripe error: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as e:
            return Response(
                {"error": f"An error occurred: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class SubscriptionSuccessView(APIView):
    """
    Handles the redirection after a successful payment.

    This endpoint corresponds to the success_url defined when creating
    a Stripe checkout session. Stripe redirects the user here when the
    payment is successful, confirming the subscription action with a
    200 OK status.
    """

    def get(self, request, *args, **kwargs):
        return Response(
            {"message": "Subscription successful"}, status=status.HTTP_200_OK
        )


class SubscriptionCancelView(APIView):
    """
    Handles the redirection after a payment failure or cancellation.

    This endpoint corresponds to the cancel_url defined
    when creating a Stripe Checkout session.
    Stripe redirects the user here
    if the payment is not completed
    (i.e., the user either cancels the payment or the payment fails for any reason).
    This confirms that the payment process was interrupted or
    not successfully completed,
    and a 200 OK status is returned to acknowledge the cancellation.
    """

    def get(self, request, *args, **kwargs):
        return Response(
            {"message": "Subscription cancelled"}, status=status.HTTP_200_OK
        )
