"""The external allocation namespace must be renewed by the restore operator."""

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from nurse.expiry_commands import close_restored_commands
from nurse.expiry_state import ExpiryError


class Command(BaseCommand):
    help = "Close restored correction authority using a new external namespace."

    def handle(self, *args, **options):
        try:
            close_restored_commands()
        except (ExpiryError, DatabaseError):
            raise CommandError("Correction reconciliation unavailable.") from None
        self.stdout.write(
            "Correction authority closed; reopening requires qualification."
        )
