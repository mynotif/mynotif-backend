from django.core.management.base import BaseCommand

from nurse.push_services import close_restored_registry, purge_registry


class Command(BaseCommand):
    help = "Purge expired B07 authority without decryption; optionally fence a restore."

    def add_arguments(self, parser):
        parser.add_argument("--close-restored", action="store_true")

    def handle(self, *args, **options):
        if options["close_restored"]:
            close_restored_registry()
        counts = purge_registry()
        self.stdout.write(
            "B07 purge: " + ", ".join(f"{key}={value}" for key, value in counts.items())
        )
