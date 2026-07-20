# Itens do plano de preventivas confirmados pelos donos (docs.md, decisão nº 20).

from django.db import migrations

ITENS = [
    # (nome, intervalo_km_padrao) — None = esporádica
    ("Troca de óleo e filtro", 10_000),
    ("Alinhamento", 10_000),
    ("Kit correia dentada + óleo da caixa", 60_000),
    ("Pneus (2 unidades)", 30_000),
    ("Suspensão", None),
    ("Embreagem", None),
    ("Jogo de velas", None),
]


def criar_itens(apps, schema_editor):
    ItemPreventiva = apps.get_model("manutencao", "ItemPreventiva")
    for nome, intervalo in ITENS:
        ItemPreventiva.objects.get_or_create(
            nome=nome, defaults={"intervalo_km_padrao": intervalo}
        )


def remover_itens(apps, schema_editor):
    ItemPreventiva = apps.get_model("manutencao", "ItemPreventiva")
    ItemPreventiva.objects.filter(nome__in=[nome for nome, _ in ITENS]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("manutencao", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(criar_itens, remover_itens),
    ]
