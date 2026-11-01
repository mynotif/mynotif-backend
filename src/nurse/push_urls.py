from django.urls import path

from nurse.push_views import (
    BindingCollectionView,
    BindingDetailView,
    ConfirmationView,
    EnrollmentDetailView,
    EnrollmentView,
)

urlpatterns = [
    path("enrollments/", EnrollmentView.as_view(), name="enrollments"),
    path(
        "enrollments/<uuid:enrollment_id>/",
        EnrollmentDetailView.as_view(),
        name="enrollment",
    ),
    path(
        "enrollments/<uuid:enrollment_id>/confirm/",
        ConfirmationView.as_view(),
        name="confirm",
    ),
    path("bindings/", BindingCollectionView.as_view(), name="bindings"),
    path("bindings/<uuid:binding_id>/", BindingDetailView.as_view(), name="binding"),
]
