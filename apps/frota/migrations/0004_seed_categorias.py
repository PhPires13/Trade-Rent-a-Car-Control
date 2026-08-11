# Categorias com os valores semanais de referência confirmados pelos donos
# (docs.md, decisão nº 15): Gol R$ 650/semana, Voyage R$ 750/semana —
# podendo variar conforme o combinado com cada motorista (o valor efetivo
# é sempre o da alocação).

from decimal import Decimal

from django.db import migrations

CATEGORIAS = [
    ("Gol", Decimal("650.00")),
    ("Voyage", Decimal("750.00")),
]


def criar_categorias(apps, schema_editor):
    Categoria = apps.get_model("frota", "Categoria")
    for nome, valor in CATEGORIAS:
        Categoria.objects.get_or_create(
            nome=nome, defaults={"valor_semanal_referencia": valor}
        )


def remover_categorias(apps, schema_editor):
    Categoria = apps.get_model("frota", "Categoria")
    Categoria.objects.filter(
        nome__in=[nome for nome, _ in CATEGORIAS], veiculos__isnull=True
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("frota", "0003_historicalveiculo_comprador_and_more"),
    ]

    operations = [
        migrations.RunPython(criar_categorias, remover_categorias),
    ]
