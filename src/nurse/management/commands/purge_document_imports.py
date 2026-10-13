"""Expire document drafts and remove only unreferenced staging objects."""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from nurse.document_storage import delete_private, list_private_prefix
from nurse.models import (
    DocumentImport,
    DocumentImportAttempt,
    DocumentImportState,
    PrescriptionDocumentAsset,
)


class Command(BaseCommand):
    help = "Expire document import drafts and purge their private staging assets."

    def handle(self, *args, **options):
        now = timezone.now()
        DocumentImportAttempt.objects.filter(
            attempted_at__lt=now - timedelta(days=1)
        ).delete()
        candidates = DocumentImport.objects.filter(
            state=DocumentImportState.ABANDONED
        ) | DocumentImport.objects.filter(expires_at__lte=now).exclude(
            state=DocumentImportState.CONFIRMED
        )
        purged = 0
        for candidate_id in candidates.values_list("pk", flat=True).iterator():
            with transaction.atomic():
                try:
                    operation = DocumentImport.objects.select_for_update().get(
                        pk=candidate_id
                    )
                except DocumentImport.DoesNotExist:
                    continue
                if (
                    operation.state != DocumentImportState.CONFIRMED
                    and operation.expires_at <= now
                ):
                    operation.state = DocumentImportState.EXPIRED
                    operation.metadata = None
                    operation.lease_token = None
                    operation.lease_expires_at = None
                    operation.save(
                        update_fields=(
                            "state",
                            "metadata",
                            "lease_token",
                            "lease_expires_at",
                            "updated_at",
                        )
                    )
                assets = list(
                    PrescriptionDocumentAsset.objects.filter(
                        document_import=operation,
                        document_version__isnull=True,
                    ).only("pk", "storage_key")
                )
                # Delete registered staging objects, then sweep any write-before-row crash.
                for asset in assets:
                    delete_private(asset.storage_key)
                prefix = f"private/document-imports/{operation.pk}"
                referenced = set(operation.assets.values_list("storage_key", flat=True))
                # Sweep orphan objects from a process crash between storage write and DB insert.
                for key in list_private_prefix(prefix):
                    if key.startswith(prefix + "/") and key not in referenced:
                        delete_private(key)
                PrescriptionDocumentAsset.objects.filter(
                    pk__in=[asset.pk for asset in assets],
                    role=PrescriptionDocumentAsset.Role.DERIVED,
                    document_version__isnull=True,
                ).delete()
                PrescriptionDocumentAsset.objects.filter(
                    pk__in=[asset.pk for asset in assets],
                    role=PrescriptionDocumentAsset.Role.ORIGINAL,
                    document_version__isnull=True,
                ).delete()
                purged += len(assets)
        self.stdout.write(f"Expired document imports; purged {purged} staging assets.")
