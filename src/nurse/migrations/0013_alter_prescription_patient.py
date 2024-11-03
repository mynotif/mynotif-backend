# Generated by Django 5.0.9 on 2024-11-03 09:19

import django.db.models.deletion
from django.db import migrations, models


def set_default_patient(apps, schema_editor):
    Prescription = apps.get_model("nurse", "Prescription")
    Prescription.objects.filter(patient__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("nurse", "0012_prescription_email_doctor"),
    ]

    operations = [
        migrations.RunPython(set_default_patient),
        migrations.AlterField(
            model_name="prescription",
            name="patient",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, to="nurse.patient"
            ),
        ),
    ]