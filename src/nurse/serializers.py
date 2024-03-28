from django.contrib.auth.models import User
from rest_framework import serializers

from nurse.models import Nurse, Patient, Prescription, UserOneSignalProfile


class PatientSerializer(serializers.ModelSerializer):
    prescriptions = serializers.SerializerMethodField()

    class Meta:
        model = Patient
        fields = "__all__"

    def get_prescriptions(self, obj):
        prescriptions = Prescription.objects.filter(patient_id=obj.id).order_by(
            "-end_date"
        )
        return PrescriptionSerializer(prescriptions, many=True).data


class PrescriptionSerializer(serializers.ModelSerializer):
    is_valid = serializers.SerializerMethodField()

    class Meta:
        model = Prescription
        fields = "__all__"
        read_only_fields = ("id", "photo_prescription")

    def get_is_valid(self, obj):
        return obj.is_valid()


class PrescriptionFileSerializer(serializers.ModelSerializer):
    class Meta:
        model = Prescription
        fields = ("id", "photo_prescription")


class NurseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Nurse
        fields = "__all__"


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
        read_only_fields = ("id",)
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


class UserOneSignalProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserOneSignalProfile
        fields = ["subscription_id", "user"]
        extra_kwargs = {"user": {"read_only": True}}
