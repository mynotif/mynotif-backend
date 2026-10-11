from django.contrib.auth.models import User
from django.core.validators import RegexValidator
from django.db import IntegrityError, transaction
from djoser.serializers import UserCreateSerializer
from drf_spectacular.utils import extend_schema_field
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
    photo_prescription = serializers.SerializerMethodField()

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

    def get_is_valid(self, obj) -> bool:
        return obj.is_valid()

    def get_expiring_soon(self, obj) -> bool:
        return obj.expiring_soon()

    def get_photo_prescription(self, obj) -> str:
        from nurse.utils.private_files import signed_file_url

        return signed_file_url(obj.photo_prescription)


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


class ProfileSerializer(serializers.ModelSerializer):
    nurse = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "is_staff",
            "nurse",
        )
        read_only_fields = ("id", "username", "email", "is_staff", "nurse")

    def to_internal_value(self, data):
        if "password" in data:
            raise serializers.ValidationError(
                {"password": ["Password changes must use the password endpoint."]}
            )
        allowed = {"first_name", "last_name"}
        forbidden = {"email", "username", "id", "is_staff", "nurse"}
        unknown = set(data) - allowed - forbidden
        if unknown:
            raise serializers.ValidationError(
                {key: ["Unknown field."] for key in sorted(unknown)}
            )
        return super().to_internal_value(
            {key: value for key, value in data.items() if key in allowed}
        )

    @extend_schema_field(NurseSerializer)
    def get_nurse(self, obj):
        # note that we're doing a `get_or_create()` rather than accessing the
        # `obj.nurse` directly, because it could be some cases where the user
        # was created without the associated nurse
        nurse, _ = Nurse.objects.get_or_create(user_id=obj.id)
        return NurseSerializer(nurse).data


class RegistrationSerializer(UserCreateSerializer):
    id = serializers.IntegerField(read_only=True)
    email = serializers.EmailField(required=True)
    first_name = serializers.CharField(required=False, allow_blank=True, max_length=150)
    last_name = serializers.CharField(required=False, allow_blank=True, max_length=150)
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    is_staff = serializers.BooleanField(read_only=True)
    nurse = serializers.SerializerMethodField()

    class Meta(UserCreateSerializer.Meta):
        fields = (
            "id",
            "email",
            "first_name",
            "last_name",
            "password",
            "is_staff",
            "nurse",
        )

    def to_internal_value(self, data):
        if "username" in data and not isinstance(self, RegistrationSerializerV1):
            raise serializers.ValidationError(
                {"username": ["This field is not allowed."]}
            )
        return super().to_internal_value(data)

    def validate(self, attrs):
        email = attrs["email"]
        if len(email) > 150:
            raise serializers.ValidationError(
                {"email": ["Email cannot exceed 150 characters."]}
            )
        username = email[:150]
        if (
            User.objects.filter(email=email).exists()
            or User.objects.filter(username=username).exists()
        ):
            raise serializers.ValidationError(
                {"email": ["This email is already in use."]}
            )
        attrs["username"] = username
        return super().validate(attrs)

    @transaction.atomic
    def create(self, validated_data):
        validated_data = dict(validated_data)
        try:
            with transaction.atomic():
                user = User.objects.create_user(**validated_data)
                Nurse.objects.get_or_create(user=user)
        except IntegrityError as exc:
            if User.objects.filter(username=validated_data["username"]).exists():
                raise serializers.ValidationError(
                    {"email": ["This email is already in use."]}
                ) from exc
            raise
        return user

    @extend_schema_field(NurseSerializer)
    def get_nurse(self, obj):
        nurse, _ = Nurse.objects.get_or_create(user=obj)
        return NurseSerializer(nurse).data


class RegistrationSerializerV1(RegistrationSerializer):
    username = serializers.CharField(max_length=150)

    class Meta(RegistrationSerializer.Meta):
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

    def validate(self, attrs):
        username = attrs["username"]
        if User.objects.filter(username=username).exists():
            raise serializers.ValidationError(
                {"username": ["This username is already in use."]}
            )
        attrs["username"] = username
        email = attrs["email"]
        if User.objects.filter(email=email).exclude(username=username).exists():
            raise serializers.ValidationError(
                {"email": ["This email is already in use."]}
            )
        return UserCreateSerializer.validate(self, attrs)


# Compatibility names retained for imports in existing extensions.
UserSerializer = ProfileSerializer
UserSerializerV2 = RegistrationSerializer


class UserOneSignalProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserOneSignalProfile
        fields = ["subscription_id", "user"]
        extra_kwargs = {"user": {"read_only": True}}
