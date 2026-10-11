from rest_framework import serializers


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(trim_whitespace=False, write_only=True)
    device_label = serializers.CharField(
        required=False, allow_blank=True, max_length=80
    )


class RefreshSerializer(serializers.Serializer):
    refresh_token = serializers.CharField()
    request_id = serializers.UUIDField()

    def validate_request_id(self, value):
        if value.version != 4:
            raise serializers.ValidationError("A UUIDv4 is required.")
        return value


class SessionOutputSerializer(serializers.Serializer):
    session_id = serializers.UUIDField()
    device_label = serializers.CharField()
    created_at = serializers.DateTimeField()
    expires_at = serializers.DateTimeField()
    last_seen_at = serializers.DateTimeField(allow_null=True)
    current = serializers.BooleanField()


class SessionPairSerializer(serializers.Serializer):
    session_id = serializers.UUIDField()
    access_token = serializers.CharField()
    refresh_token = serializers.CharField()
    token_type = serializers.ChoiceField(choices=["Bearer"])
    access_expires_at = serializers.DateTimeField()
    expires_in = serializers.IntegerField(min_value=0)
    refresh_expires_at = serializers.DateTimeField()
    session_expires_at = serializers.DateTimeField()


class SessionErrorSerializer(serializers.Serializer):
    code = serializers.CharField()
    message = serializers.CharField()


class PrivateFileURLSerializer(serializers.Serializer):
    url = serializers.URLField()
    expires_at = serializers.DateTimeField()
    expires_in = serializers.IntegerField(min_value=1, max_value=900)
