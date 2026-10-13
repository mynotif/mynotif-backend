from django.db import migrations, models


class AddSearchField(migrations.AddField):
    """Fail fast on PostgreSQL metadata locks in both migration directions."""

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == "postgresql":
            schema_editor.execute("SET LOCAL lock_timeout = '2s'")
        super().database_forwards(app_label, schema_editor, from_state, to_state)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == "postgresql":
            schema_editor.execute("SET LOCAL lock_timeout = '2s'")
        super().database_backwards(app_label, schema_editor, from_state, to_state)


class Migration(migrations.Migration):
    dependencies = [("nurse", "0013_alter_prescription_patient")]

    operations = [
        AddSearchField(
            model_name="patient",
            name=name,
            field=models.TextField(editable=False, null=True),
        )
        for name in (
            "search_firstname",
            "search_lastname",
            "search_city",
            "search_phone",
        )
    ]
