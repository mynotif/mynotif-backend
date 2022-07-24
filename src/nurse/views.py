from django.contrib.auth.models import User
from rest_framework import generics, mixins, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from nurse.models import Nurse, Patient, Prescription
from nurse.serializers import (
    NurseSerializer,
    PatientSerializer,
    PrescriptionFileSerializer,
    PrescriptionSerializer,
    UserSerializer,
)


class PatientViewSet(viewsets.ModelViewSet):
    queryset = Patient.objects.all()
    serializer_class = PatientSerializer


class PrescriptionViewSet(viewsets.ModelViewSet):
    queryset = Prescription.objects.all()
    serializer_class = PrescriptionSerializer

    def get_queryset(self):
        queryset = self.queryset
        nurse, _ = Nurse.objects.get_or_create(
            user=self.request.user,
        )
        query_set = queryset.filter(patient__nurse=nurse)
        return query_set


class PrescriptionFileView(generics.UpdateAPIView):
    queryset = Prescription.objects.all()
    serializer_class = PrescriptionFileSerializer


class NurseViewSet(viewsets.ModelViewSet):
    queryset = Nurse.objects.all()
    serializer_class = NurseSerializer


class ProfileView(APIView):
    def get(self, request):
        request.user
        serializer = UserSerializer(request.user)
        return Response(serializer.data)


class UserViewSet(
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    """
    Retrieve, update, destroy and list, but no create.
    For Create, see `UserCreate` below.
    """

    queryset = User.objects.all()
    serializer_class = UserSerializer

    def get_object(self):
        """Fecthing a specific object other than `request.user` isn't allowed."""
        if self.kwargs.get("pk").isdigit():
            raise PermissionDenied()
        return self.request.user


class UserCreate(generics.CreateAPIView):
    queryset = User.objects.all()
    serializer_class = UserSerializer
    permission_classes = (AllowAny,)

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        username = response.data["username"]
        user = User.objects.get(username=username)
        Nurse.objects.get_or_create(
            user=user,
        )
        return response
