from django.apps import AppConfig


class MainConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "main"

    def ready(self):
        import main.schema  # noqa: F401
        from nurse.read_privacy import install_log_redaction

        install_log_redaction()
