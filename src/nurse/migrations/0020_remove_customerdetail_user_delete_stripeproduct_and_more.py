# Generated by Django 5.1.3 on 2024-11-24 11:00

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("nurse", "0019_rename_customerdetails_customerdetail"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="customerdetail",
            name="user",
        ),
        migrations.DeleteModel(
            name="StripeProduct",
        ),
        migrations.RemoveField(
            model_name="subscription",
            name="user",
        ),
        migrations.DeleteModel(
            name="CustomerDetail",
        ),
        migrations.DeleteModel(
            name="Subscription",
        ),
    ]
