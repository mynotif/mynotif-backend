from django.contrib import admin

from payment.models import CustomerDetail, StripeProduct, Subscription


@admin.register(StripeProduct)
class StripeProductAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "product_id",
        "monthly_price_id",
        "annual_price_id",
    )


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = list_display = [
        field.name for field in Subscription._meta.get_fields()
    ]


@admin.register(CustomerDetail)
class CustomerDetailAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "stripe_customer_id",
        "city",
        "country",
        "address",
        "postal_code",
        "email",
    )
