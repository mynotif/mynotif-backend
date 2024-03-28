from django.contrib.auth.models import User
from django.shortcuts import get_object_or_404
from rest_framework import generics, mixins, status, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from nurse.management.commands._notifications import notify
from nurse.models import Nurse, Patient, Prescription, UserOneSignalProfile
from nurse.serializers import (
    NurseSerializer,
    PatientSerializer,
    PrescriptionFileSerializer,
    PrescriptionSerializer,
    UserOneSignalProfileSerializer,
    UserSerializer,
)


class PatientViewSet(viewsets.ModelViewSet):
    queryset = Patient.objects.all()
    serializer_class = PatientSerializer

    def get_queryset(self):
        """Only the patients associated to the logged in nurse."""
        queryset = self.queryset
        nurse, _ = Nurse.objects.get_or_create(user=self.request.user)
        queryset = queryset.filter(nurse=nurse)
        return queryset

    def create(self, request):
        nurse, _ = Nurse.objects.get_or_create(user=self.request.user)
        response = super().create(request)
        patient = Patient.objects.get(id=response.data["id"])
        patient.nurse_set.add(nurse)
        return response


class PrescriptionViewSet(viewsets.ModelViewSet):
    queryset = Prescription.objects.all()
    serializer_class = PrescriptionSerializer

    def get_queryset(self):
        """Only the prescriptions associated to the logged in nurse."""
        queryset = self.queryset
        nurse, _ = Nurse.objects.get_or_create(user=self.request.user)
        queryset = queryset.filter(patient__nurse=nurse)
        return queryset


class PrescriptionFileView(generics.UpdateAPIView):
    queryset = Prescription.objects.all()
    serializer_class = PrescriptionFileSerializer


class NurseViewSet(viewsets.ModelViewSet):
    queryset = Nurse.objects.all()
    serializer_class = NurseSerializer


class UserOneSignalProfileViewSet(mixins.CreateModelMixin, viewsets.GenericViewSet):
    queryset = UserOneSignalProfile.objects.all()
    serializer_class = UserOneSignalProfileSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return UserOneSignalProfile.objects.filter(user=self.request.user)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(user=request.user)
        headers = self.get_success_headers(serializer.data)
        return Response(
            serializer.data, status=status.HTTP_201_CREATED, headers=headers
        )

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())

        profile = queryset.first()
        if profile:
            serializer = self.get_serializer(profile)
            return Response([serializer.data])
        return Response([])

    def retrieve(self, request, *args, **kwargs):
        obj = get_object_or_404(self.get_queryset(), user=request.user)
        serializer = self.get_serializer(obj)
        return Response(serializer.data)


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
        Nurse.objects.get_or_create(user=user)
        return response


class AdminNotificationView(APIView):
    """The view dealing with sending push notifications."""

    permission_classes = [IsAuthenticated, IsAdminUser]

    def post(self, request):
        notify()
        return Response(status=status.HTTP_204_NO_CONTENT)
