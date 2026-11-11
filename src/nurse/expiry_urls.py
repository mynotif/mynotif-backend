from django.urls import path

from nurse.expiry_views import ExpiryPrescriptionDetail, ExpiryPrescriptionList

urlpatterns = [
    path(
        "prescriptions/", ExpiryPrescriptionList.as_view(), name="expiry-prescriptions"
    ),
    path(
        "patients/<str:patient_id>/prescriptions/<str:prescription_id>/",
        ExpiryPrescriptionDetail.as_view(),
        name="expiry-prescription-detail",
    ),
]

from nurse.expiry_command_views import (  # noqa: E402
    ExpiryCommandDetail,
    ExpiryCommands,
    ExpiryDateCorrection,
    ExpiryReconciliationFence,
    ExpiryRenewalConfirmation,
)

urlpatterns += [
    path("commands/", ExpiryCommands.as_view(), name="expiry-commands"),
    path(
        "commands/<str:command_id>/",
        ExpiryCommandDetail.as_view(),
        name="expiry-command-detail",
    ),
    path(
        "commands/<str:command_id>/date-correction/",
        ExpiryDateCorrection.as_view(),
        name="expiry-date-correction",
    ),
    path(
        "commands/<str:command_id>/renewal-confirmation/",
        ExpiryRenewalConfirmation.as_view(),
        name="expiry-renewal-confirmation",
    ),
    path(
        "reconciliation-fence/",
        ExpiryReconciliationFence.as_view(),
        name="expiry-reconciliation-fence",
    ),
]
