from django.contrib.auth.models import User
from django.core.validators import RegexValidator
from rest_framework import serializers

from nurse.models import Nurse, Patient, Prescription, UserOneSignalProfile


class DynamicFieldsModelSerializer(serializers.ModelSerializer):
    """
    A ModelSerializer that takes an additional `fields` argument that controls which
    fields should be displayed.
    https://github.com/encode/django-rest-framework/blob/3.15.2/docs/api-guide/serializers.md#dynamically-modifying-fields
    """

    def __init__(self, *args, **kwargs):
        # Don't pass the 'fields' arg up to the superclass
        fields = kwargs.pop("fields", None)

        # Instantiate the superclass normally
        super().__init__(*args, **kwargs)

        if fields is not None:
            # Drop any fields that are not specified in the `fields` argument.
            allowed = set(fields)
            existing = set(self.fields)
            for field_name in existing - allowed:
                self.fields.pop(field_name)


class PatientSerializer(DynamicFieldsModelSerializer):
    prescriptions = serializers.SerializerMethodField()
    expire_soon_prescriptions = serializers.SerializerMethodField()

    class Meta:
        model = Patient
        fields = "__all__"

    def get_prescriptions(self, obj):
        prescriptions = Prescription.objects.filter(patient_id=obj.id).order_by(
            "-end_date"
        )
        return PrescriptionSerializer(prescriptions, many=True).data

    def get_expire_soon_prescriptions(self, obj):
        prescriptions = Prescription.objects.expiring_soon()
        prescriptions = prescriptions.filter(patient_id=obj.id).order_by("-end_date")
        return PrescriptionSerializer(prescriptions, many=True).data


class PrescriptionEmailSerializer(serializers.Serializer):
    additional_info = serializers.CharField(
        required=True,
        max_length=1000,
        validators=[
            RegexValidator(
                regex=r"^(?!.*[<>]).*$",
            )
        ],
    )


class PrescriptionSerializer(DynamicFieldsModelSerializer):
    is_valid = serializers.SerializerMethodField()
    expiring_soon = serializers.SerializerMethodField()

    class Meta:
        model = Prescription
        fields = "__all__"
        read_only_fields = ("id", "photo_prescription")

    def get_fields(self):
        fields = super().get_fields()
        user = getattr(self.context.get("request"), "user", None)
        patients = Patient.objects.none()
        if user is not None and user.is_authenticated:
            patients = Patient.objects.filter(nurse__user=user)
        fields["patient"].queryset = patients
        return fields

    def get_is_valid(self, obj):
        return obj.is_valid()

    def get_expiring_soon(self, obj):
        return obj.expiring_soon()


class ExpandedPrescriptionSerializer(PrescriptionSerializer):
    """Serializer for prescription details, including patient's first and last names."""

    patient_firstname = serializers.CharField(
        source="patient.firstname", read_only=True
    )
    patient_lastname = serializers.CharField(source="patient.lastname", read_only=True)


class PrescriptionFileSerializer(serializers.ModelSerializer):
    class Meta:
        model = Prescription
        fields = ("id", "photo_prescription")


class NurseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Nurse
        fields = "__all__"
        read_only_fields = ("id", "user", "patients")


class UserSerializer(serializers.ModelSerializer):
    nurse = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "password",
            "is_staff",
            "nurse",
        )
        read_only_fields = ("id", "is_staff")
        extra_kwargs = {"password": {"write_only": True}}

    def create(self, validated_data):
        password = validated_data.pop("password")
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        return user

    def get_nurse(self, obj):
        # note that we're doing a `get_or_create()` rather than accessing the
        # `obj.nurse` directly, because it could be some cases where the user
        # was created without the associated nurse
        nurse, _ = Nurse.objects.get_or_create(user_id=obj.id)
        return NurseSerializer(nurse).data


class UserSerializerV2(UserSerializer):
    """
    V2 serializer inherits from V1 but excludes `username` field.
    Automatically sets `username` to the provided `email`.
    """

    class Meta(UserSerializer.Meta):
        """Exclude `username` from fields."""

        fields = (
            "id",
            "email",
            "first_name",
            "last_name",
            "password",
            "is_staff",
            "nurse",
        )

    def create(self, validated_data):
        """Automatically set `username` to `email`."""
        validated_data["username"] = validated_data["email"]
        return super().create(validated_data)


class UserOneSignalProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserOneSignalProfile
        fields = ["subscription_id", "user"]
        extra_kwargs = {"user": {"read_only": True}}
