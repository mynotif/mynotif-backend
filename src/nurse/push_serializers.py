import re

from rest_framework import serializers

from nurse.push_crypto import InvalidProof, public_key


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) - set(self.fields):
            raise serializers.ValidationError("invalid_request")
        return super().to_internal_value(data)


class EnrollmentSerializer(StrictSerializer):
    operation = serializers.ChoiceField(choices=("enroll", "rotate"))
    request_id = serializers.UUIDField()
    namespace = serializers.RegexField(r"\A[A-Za-z0-9_.:-]{1,120}\Z")
    token = serializers.CharField(
        max_length=4096, trim_whitespace=False, write_only=True
    )
    public_key = serializers.CharField(
        max_length=87, trim_whitespace=False, write_only=True
    )
    permission = serializers.ChoiceField(choices=("granted", "denied"))
    previous_binding_id = serializers.UUIDField(required=False)

    def validate_token(self, value):
        if (
            not re.fullmatch(r"[A-Za-z0-9_:.~-]{1,4096}", value)
            or "ExpoPushToken" in value
            or "ExponentPushToken" in value
        ):
            raise serializers.ValidationError("invalid_request")
        return value

    def validate_public_key(self, value):
        try:
            public_key(value)
        except InvalidProof:
            raise serializers.ValidationError("invalid_request") from None
        return value

    def validate(self, attrs):
        if (attrs["operation"] == "rotate") != ("previous_binding_id" in attrs):
            raise serializers.ValidationError("invalid_request")
        return attrs


class ConfirmationSerializer(StrictSerializer):
    nonce = serializers.CharField(max_length=43, trim_whitespace=False, write_only=True)
    signature = serializers.CharField(
        max_length=96, trim_whitespace=False, write_only=True
    )
