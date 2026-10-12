from rest_framework import serializers

from nurse.models import Prescription
from nurse.read_serializers import ReadQuerySerializer


class PrescriptionSummarySerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(read_only=True, min_value=1)
    patient_id = serializers.IntegerField(read_only=True, min_value=1)

    class Meta:
        model = Prescription
        fields = ("id", "patient_id", "prescribing_doctor", "start_date", "end_date")
        read_only_fields = fields


class PrescriptionPageSerializer(serializers.Serializer):
    count = serializers.IntegerField(min_value=0)
    page = serializers.IntegerField(min_value=1, max_value=2147483647)
    page_size = serializers.IntegerField(min_value=1, max_value=100)
    next_page = serializers.IntegerField(min_value=1, allow_null=True)
    previous_page = serializers.IntegerField(min_value=1, allow_null=True)
    results = PrescriptionSummarySerializer(many=True)


class PrescriptionDetailQuerySerializer(ReadQuerySerializer):
    page = None
    page_size = None
