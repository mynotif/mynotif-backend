# Generated by Django 3.2.5 on 2021-07-09 09:12

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('nurse', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='nurse',
            name='phone',
            field=models.CharField(max_length=30),
        ),
    ]
