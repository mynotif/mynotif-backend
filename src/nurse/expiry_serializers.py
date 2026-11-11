"""Closed wire schemas for expiry v4. Legacy model serializers stay untouched."""

from drf_spectacular.openapi import AutoSchema
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from nurse.expiry_state import ERRORS
from nurse.read_serializers import PositiveQueryInteger

FILTERS = ("all", "to_renew", "expired", "unknown", "future", "invalid", "renewed")
ORDERINGS = ("end_date_asc", "end_date_desc")


class ExpirySchema(AutoSchema):
    def _map_serializer(self, serializer, direction, bypass_extensions=False):
        result = super()._map_serializer(serializer, direction, bypass_extensions)
        if serializer.__class__.__module__ == __name__:
            result["additionalProperties"] = False
            result["required"] = sorted(result.get("properties", {}))
        return result


class ExpiryQuerySerializer(serializers.Serializer):
    filter = serializers.ChoiceField(choices=FILTERS, default="all")
    ordering = serializers.ChoiceField(choices=ORDERINGS, default="end_date_asc")
    page_size = PositiveQueryInteger(maximum=100, default=20)
    cursor = serializers.CharField(
        max_length=2048, required=False, trim_whitespace=False
    )


@extend_schema_field(
    {"type": "string", "enum": list(ERRORS)}, component_name="ExpiryErrorCode"
)
class ExpiryErrorCodeField(serializers.ChoiceField):
    pass


@extend_schema_field({"type": "object", "additionalProperties": False})
class ExpiryErrorFields(serializers.DictField):
    """Reads currently emit no per-field diagnostics."""


class ExpiryErrorSerializer(serializers.Serializer):
    code = ExpiryErrorCodeField(choices=tuple(ERRORS))
    message = serializers.CharField()
    fields = ExpiryErrorFields(
        child=serializers.ListField(
            child=serializers.ChoiceField(
                choices=("required", "invalid", "unknown", "inconsistent", "not_true")
            )
        )
    )


class ExpiryContextSerializer(serializers.Serializer):
    business_date = serializers.DateField()
    timezone = serializers.ChoiceField(choices=("Europe/Paris",))
    validated_at = serializers.DateTimeField()
    next_day_at = serializers.DateTimeField()
    revalidate_in_ms = serializers.IntegerField(min_value=0)
    scope_revision = serializers.CharField(max_length=128)
    collection_revision = serializers.CharField(max_length=128)
    calculation_version = serializers.CharField(max_length=128)
    classification_version = serializers.CharField(max_length=128)
    mutation_epoch = serializers.CharField(max_length=128)


class ExpiryPatientSerializer(serializers.Serializer):
    id = serializers.IntegerField(min_value=1, max_value=9007199254740991)
    firstname = serializers.CharField(max_length=150, allow_blank=True)
    lastname = serializers.CharField(max_length=150, allow_blank=True)


class ExpiryReplacementSerializer(serializers.Serializer):
    prescription_id = serializers.IntegerField(min_value=1, max_value=9007199254740991)
    confirmed_at = serializers.DateTimeField()


class ExpiryItemSerializer(serializers.Serializer):
    id = serializers.IntegerField(min_value=1, max_value=9007199254740991)
    patient = ExpiryPatientSerializer()
    prescribing_doctor = serializers.CharField(max_length=300, allow_blank=True)
    start_date = serializers.DateField(allow_null=True)
    end_date = serializers.DateField(allow_null=True)
    revision = serializers.CharField(max_length=128)
    date_state = serializers.ChoiceField(
        choices=("unknown", "invalid", "future", "valid", "expired")
    )
    to_renew = serializers.BooleanField()
    proximity = serializers.ChoiceField(choices=("none", "near"))
    followup_state = serializers.ChoiceField(
        choices=("to_process", "in_progress", "renewed")
    )
    replacement = ExpiryReplacementSerializer(allow_null=True)
    document_version = serializers.IntegerField(min_value=1, allow_null=True)


class ExpiryPageSerializer(serializers.Serializer):
    contract_revision = serializers.ChoiceField(choices=("0.2-decision",))
    context = ExpiryContextSerializer()
    filter = serializers.ChoiceField(choices=FILTERS)
    ordering = serializers.ChoiceField(choices=ORDERINGS)
    page_size = serializers.IntegerField(min_value=1, max_value=100)
    count = serializers.IntegerField(min_value=0)
    next_cursor = serializers.CharField(max_length=2048, allow_null=True)
    results = ExpiryItemSerializer(many=True)


class ExpiryDetailSerializer(serializers.Serializer):
    contract_revision = serializers.ChoiceField(choices=("0.2-decision",))
    context = ExpiryContextSerializer()
    item = ExpiryItemSerializer()
    can_correct_dates = serializers.BooleanField()
    can_confirm_renewal = serializers.BooleanField()
