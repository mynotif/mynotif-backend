from rest_framework import serializers

from payment.models import Subscription


class SubscriptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Subscription
        fields = "__all__"
        extra_kwargs = {
            "user": {"read_only": True},
            "stripe_subscription_id": {"required": False},
        }
