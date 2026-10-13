from django.urls import path

from nurse.document_dispatch import import_view

urlpatterns = [
    path(
        "document-capabilities/",
        import_view("capabilities"),
        name="document-capabilities",
    ),
    path(
        "patients/<int:patient_id>/document-imports/",
        import_view("imports"),
        name="document-imports",
    ),
    path(
        "patients/<int:patient_id>/document-imports/<uuid:import_uuid>/",
        import_view("import"),
        name="document-import",
    ),
    path(
        "patients/<int:patient_id>/document-imports/<uuid:import_uuid>/confirmation/",
        import_view("confirmation"),
        name="document-import-confirmation",
    ),
    path(
        "patients/<int:patient_id>/document-imports/<uuid:import_uuid>/assets/"
        "<uuid:asset_id>/content/",
        import_view("import-asset-content"),
        name="document-import-asset-content",
    ),
]
