from django.urls import path
from rest_framework import routers

from nurse.views import (
    AdminNotificationView,
    NurseViewSet,
    PatientViewSet,
    PrescriptionFileView,
    PrescriptionViewSet,
    ProfileView,
    UserOneSignalProfileViewSet,
    UserViewSet,
)

router = routers.DefaultRouter()
router.register("patient", PatientViewSet)
router.register("prescription", PrescriptionViewSet)
router.register("nurse", NurseViewSet)
router.register("user", UserViewSet)
router.register("onesignal", UserOneSignalProfileViewSet)

urlpatterns = [
    path("profile/", ProfileView.as_view(), name="profile"),
    path(
        "prescription/upload/<int:pk>/",
        PrescriptionFileView.as_view(),
        name="prescription-upload",
    ),
    path("notify/", AdminNotificationView.as_view(), name="notify"),
]

urlpatterns += router.urls
