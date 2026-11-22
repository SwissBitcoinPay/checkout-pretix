import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    # OrderPayment's id is a bigint from this one on (the payment column below follows it), in
    # every pretix version this plugin supports.
    dependencies = [
        ("pretixbase", "0246_bigint"),
    ]

    operations = [
        migrations.CreateModel(
            name="BitcoinInvoice",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False
                    ),
                ),
                ("ref", models.CharField(max_length=64, unique=True)),
                ("data", models.JSONField(blank=True, null=True)),
                ("status", models.JSONField(null=True)),
                ("final", models.BooleanField(default=False)),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("checked", models.DateTimeField(blank=True, null=True)),
                ("next_check", models.DateTimeField(blank=True, null=True)),
                ("chain", models.CharField(max_length=32)),
                ("lease_until", models.DateTimeField(blank=True, null=True)),
                ("error", models.CharField(max_length=64)),
                ("errors", models.PositiveIntegerField(default=0)),
                ("shredded", models.BooleanField(default=False)),
                (
                    "payment",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="sattleme_invoice",
                        to="pretixbase.orderpayment",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["final", "next_check"],
                        name="pretix_satt_final_4d77ed_idx",
                    )
                ],
            },
        ),
    ]
