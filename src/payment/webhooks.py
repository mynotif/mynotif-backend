import logging

import stripe
from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt

from .stripe_event_handlers import (
    handle_checkout_session_completed,
    handle_customer_subscription_deleted,
    handle_customer_subscription_updated,
    handle_invoice_paid,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_API_KEY
STRIPE_WEBHOOK_SECRET = settings.STRIPE_WEBHOOK_SECRET


@csrf_exempt
def stripe_webhook(request):
    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")
    event = None

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        return HttpResponse(status=400)

    if event["type"] == "checkout.session.completed":
        handle_checkout_session_completed(event)
    elif event["type"] == "customer.subscription.updated":
        handle_customer_subscription_updated(event)
    elif event["type"] == "invoice.paid":
        handle_invoice_paid(event)
    elif event["type"] == "customer.subscription.deleted":
        handle_customer_subscription_deleted(event)

    return HttpResponse(status=200)
