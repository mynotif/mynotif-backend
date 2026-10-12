"""Closed schemas for the M03 read contract; never reuse document serializers."""

import re

from rest_framework import serializers

from nurse.models import Patient


class PositiveQueryInteger(serializers.Field):
    def __init__(self, maximum, **kwargs):
        self.maximum = maximum
        super().__init__(**kwargs)

    def to_internal_value(self, value):
        if (
            not isinstance(value, str)
            or not re.fullmatch(r"[1-9][0-9]*", value, flags=re.ASCII)
            or len(value) > len(str(self.maximum))
            or int(value) > self.maximum
        ):
            raise serializers.ValidationError("Invalid query.")
        return int(value)

    def to_representation(self, value):
        return value


class SearchQuery(serializers.Field):
    def to_internal_value(self, value):
        if not isinstance(value, str) or len(value) > 100:
            raise serializers.ValidationError("Invalid query.")
        return value

    def to_representation(self, value):
        return value


class ReadQuerySerializer(serializers.Serializer):
    page = PositiveQueryInteger(maximum=2147483647, default=1)
    page_size = PositiveQueryInteger(maximum=100, default=20)

    def to_internal_value(self, data):
        if any(key not in self.fields or len(data.getlist(key)) != 1 for key in data):
            raise serializers.ValidationError({"non_field_errors": ["Invalid query."]})
        return super().to_internal_value({key: data[key] for key in data})


class PatientQuerySerializer(ReadQuerySerializer):
    search = SearchQuery(default="")


class PatientSummarySerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(read_only=True, min_value=1)

    class Meta:
        model = Patient
        fields = ("id", "firstname", "lastname", "city", "phone", "birthday")
        read_only_fields = fields


class ReadErrorSerializer(serializers.Serializer):
    code = serializers.ChoiceField(
        choices=(
            "invalid_query",
            "authentication_required",
            "not_found",
            "page_not_found",
            "method_not_allowed",
            "rate_limited",
            "service_unavailable",
        )
    )
    message = serializers.CharField()


class PatientPageSerializer(serializers.Serializer):
    count = serializers.IntegerField(min_value=0)
    page = serializers.IntegerField(min_value=1, max_value=2147483647)
    page_size = serializers.IntegerField(min_value=1, max_value=100)
    next_page = serializers.IntegerField(min_value=1, allow_null=True)
    previous_page = serializers.IntegerField(min_value=1, allow_null=True)
    results = PatientSummarySerializer(many=True)
