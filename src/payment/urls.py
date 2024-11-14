from django.urls import include, path
from rest_framework import routers

from payment.views import (
    SubscriptionCancelView,
    SubscriptionSuccessView,
    SubscriptionUserCancelView,
    SubscriptionViewSet,
)

from . import webhooks

router = routers.DefaultRouter()
router.register("subscription", SubscriptionViewSet)

app_name = "payment"

urlpatterns = [
    path("", include(router.urls)),
    path(
        "subscriptions/success/",
        SubscriptionSuccessView.as_view(),
        name="subscription-success",
    ),
    path(
        "subscriptions/cancel/",
        SubscriptionCancelView.as_view(),
        name="subscription-cancel",
    ),
    path(
        "subscriptions/user/cancel/",
        SubscriptionUserCancelView.as_view(),
        name="subscription-user-cancel",
    ),
    path("stripe/webhook/", webhooks.stripe_webhook, name="stripe-webhook"),
]

urlpatterns += router.urls
