from datetime import datetime

from django.utils import timezone

from helpers.model_utils import get_object_or_400
from payment.models import CustomerDetail, Subscription, User


def handle_checkout_session_completed(event):
    """
    Handles the "checkout.session.completed" Stripe event.
    Updates or creates customer details and subscription information.
    """
    session = event["data"]["object"]
    user = get_object_or_400(User, id=session["metadata"]["user_id"])
    CustomerDetail.objects.update_or_create(
        user=user,
        defaults={
            "stripe_customer_id": session["customer"],
            "city": session["customer_details"]["address"]["city"],
            "country": session["customer_details"]["address"]["country"],
            "address": session["customer_details"]["address"]["line1"],
            "postal_code": session["customer_details"]["address"]["postal_code"],
            "email": session["customer_details"]["email"],
        },
    )
    Subscription.objects.update_or_create(
        user=user,
        defaults={
            "stripe_subscription_id": session["subscription"],
            "status": session["status"],
            "payment_status": session["payment_status"],
            "product_name": session["metadata"]["product_name"],
            "total_price": session["amount_total"] / 100,
        },
    )


def handle_customer_subscription_updated(event):
    """
    Handles the "customer.subscription.updated" Stripe event.
    Updates the subscription information for the user.
    """
    subscription_updated = event["data"]["object"]
    customer = get_object_or_400(
        CustomerDetail, stripe_customer_id=subscription_updated["customer"]
    )
    user = customer.user
    Subscription.objects.filter(user=user).update(
        cancel_at_period_end=subscription_updated["cancel_at_period_end"],
        current_period_start=timezone.make_aware(
            datetime.fromtimestamp(subscription_updated["current_period_start"])
        ),
        current_period_end=timezone.make_aware(
            datetime.fromtimestamp(subscription_updated["current_period_end"])
        ),
        trial_end=(
            timezone.make_aware(
                datetime.fromtimestamp(subscription_updated["trial_end"])
            )
            if subscription_updated["trial_end"]
            and subscription_updated["trial_end"] != "null"
            else None
        ),
        active=bool(subscription_updated["items"]["data"][0]["plan"]["active"]),
    )


def handle_invoice_paid(event):
    """
    Handles the "invoice.paid" Stripe event.
    Updates the invoice information for the user's subscription.
    """
    invoice_paid = event["data"]["object"]
    customer = get_object_or_400(
        CustomerDetail, stripe_customer_id=invoice_paid["customer"]
    )
    user = customer.user
    Subscription.objects.filter(user=user).update(
        hosted_invoice_url=invoice_paid["hosted_invoice_url"],
        invoice_pdf=invoice_paid["invoice_pdf"],
    )


def handle_customer_subscription_deleted(event):
    """
    Handles the "customer.subscription.deleted" Stripe event.
    Updates the subscription status and active state for the user.
    """
    subscription_deleted = event["data"]["object"]
    customer = get_object_or_400(
        CustomerDetail, stripe_customer_id=subscription_deleted["customer"]
    )
    user = customer.user
    Subscription.objects.filter(user=user).update(
        status=subscription_deleted.get("status"), active=False
    )


def handle_default(event):
    """No operation for unhandled events"""
    pass
