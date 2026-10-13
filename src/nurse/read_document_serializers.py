from rest_framework import serializers

from nurse.read_prescription_serializers import PrescriptionSummarySerializer


class DocumentAssetSerializer(serializers.Serializer):
    asset_id = serializers.UUIDField()
    mime_type = serializers.ChoiceField(choices=("image/jpeg", "image/png", "application/pdf"))
    byte_size = serializers.IntegerField(min_value=1)
    page_count = serializers.IntegerField(min_value=1)
    sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    role = serializers.ChoiceField(choices=("original", "derived"))
    original_index = serializers.IntegerField(allow_null=True, min_value=0)
    source_asset_id = serializers.UUIDField(allow_null=True)


class CropSerializer(serializers.Serializer):
    x = serializers.FloatField(min_value=0, max_value=1)
    y = serializers.FloatField(min_value=0, max_value=1)
    width = serializers.FloatField(min_value=0, max_value=1)
    height = serializers.FloatField(min_value=0, max_value=1)


class DocumentPageSerializer(serializers.Serializer):
    source_asset_id = serializers.UUIDField()
    source_page = serializers.IntegerField(min_value=1)
    display_asset_id = serializers.UUIDField()
    display_page = serializers.IntegerField(min_value=1)
    rotation = serializers.ChoiceField(choices=(0, 90, 180, 270))
    crop = CropSerializer(allow_null=True)


class DocumentManifestSerializer(serializers.Serializer):
    version = serializers.IntegerField(min_value=1)
    assets = DocumentAssetSerializer(many=True)
    pages = DocumentPageSerializer(many=True)


class DocumentReadErrorSerializer(serializers.Serializer):
    code = serializers.ChoiceField(
        choices=(
            "invalid_query", "authentication_required", "not_found",
            "page_not_found", "method_not_allowed", "rate_limited",
            "service_unavailable",
        )
    )
    message = serializers.CharField()
    fields = serializers.DictField(
        child=serializers.ListField(child=serializers.CharField()), allow_empty=True
    )


class PrescriptionV3PageSerializer(serializers.Serializer):
    count = serializers.IntegerField(min_value=0)
    page = serializers.IntegerField(min_value=1)
    page_size = serializers.IntegerField(min_value=1, max_value=100)
    next_page = serializers.IntegerField(allow_null=True, min_value=1)
    previous_page = serializers.IntegerField(allow_null=True, min_value=1)
    results = PrescriptionSummarySerializer(many=True)
