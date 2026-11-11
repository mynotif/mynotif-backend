"""Purge only terminal technical results after fencing their actor's epoch."""

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from nurse.expiry_commands import purge_commands
from nurse.expiry_state import ExpiryError


class Command(BaseCommand):
    help = "Fence and purge correction results older than thirty terminal days."

    def handle(self, *args, **options):
        try:
            purge_commands()
        except (ExpiryError, DatabaseError):
            raise CommandError("Correction reconciliation unavailable.") from None
        self.stdout.write("Correction retention complete.")
