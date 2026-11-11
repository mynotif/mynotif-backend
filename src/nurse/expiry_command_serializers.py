"""Strict JSON and closed correction wire schemas; no legacy coercions."""

import json
import re
from datetime import date

from django.core.validators import MaxLengthValidator
from rest_framework import serializers
from rest_framework.exceptions import ParseError
from rest_framework.parsers import JSONParser

from nurse.expiry_serializers import ExpiryErrorSerializer


class StrictJSONParser(JSONParser):
    def parse(self, stream, media_type=None, parser_context=None):
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError
                result[key] = value
            return result

        def nonfinite(value):
            raise ValueError

        try:
            return json.load(stream, object_pairs_hook=pairs, parse_constant=nonfinite)
        except (ValueError, UnicodeError):
            raise ParseError() from None


class ClosedSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if type(data) is not dict or set(data) != set(self.fields):
            raise serializers.ValidationError("invalid")
        return super().to_internal_value(data)


class StrictId(serializers.IntegerField):
    def __init__(self, **kwargs):
        super().__init__(min_value=1, max_value=9007199254740991, **kwargs)

    def to_internal_value(self, data):
        if type(data) is not int:
            self.fail("invalid")
        return super().to_internal_value(data)


class Revision(serializers.CharField):
    def __init__(self, **kwargs):
        super().__init__(max_length=128, trim_whitespace=False, **kwargs)

    def to_internal_value(self, data):
        if type(data) is not str:
            self.fail("invalid")
        return super().to_internal_value(data)


class Civil(serializers.DateField):
    def to_internal_value(self, data):
        if type(data) is not str or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}", data
        ):
            self.fail("invalid", format="YYYY-MM-DD")
        try:
            date.fromisoformat(data)
        except ValueError:
            self.fail("invalid", format="YYYY-MM-DD")
        # Keep the exact civil wire value for canonical immutable admission.
        return data


class ExpiryCorrectionSerializer(ClosedSerializer):
    mutation_epoch = Revision()
    patient_id = StrictId()
    prescription_id = StrictId()
    expected_revision = Revision()
    start_date = Civil(allow_null=True)
    end_date = Civil(allow_null=True)


class ExpiryAdmissionSerializer(ClosedSerializer):
    kind = serializers.ChoiceField(choices=("date_correction",))
    intent = ExpiryCorrectionSerializer()


class ExpiryFenceRequestSerializer(ClosedSerializer):
    pass


class ExpiryCommandResultSerializer(serializers.Serializer):
    patient_id = StrictId()
    prescription_id = StrictId()
    revision = Revision()
    replacement_prescription_id = StrictId(allow_null=True)
    collection_revision = Revision()


class ExpiryCommandSerializer(serializers.Serializer):
    command_id = serializers.UUIDField()
    mutation_epoch = Revision()
    kind = serializers.ChoiceField(choices=("date_correction",))
    state = serializers.ChoiceField(choices=("pending", "succeeded", "rejected"))
    recorded_at = serializers.DateTimeField()
    result = ExpiryCommandResultSerializer(allow_null=True)
    error = ExpiryErrorSerializer(allow_null=True)


class ExpiryCommandPageSerializer(serializers.Serializer):
    contract_revision = serializers.ChoiceField(choices=("0.2-decision",))
    mutation_epoch = Revision()
    journal_revision = Revision()
    next_cursor = serializers.CharField(max_length=2048, allow_null=True)
    results = serializers.ListSerializer(
        child=ExpiryCommandSerializer(), validators=[MaxLengthValidator(20)]
    )


class ExpiryFenceSerializer(serializers.Serializer):
    contract_revision = serializers.ChoiceField(choices=("0.2-decision",))
    mutation_epoch = Revision()
    journal_revision = Revision()
