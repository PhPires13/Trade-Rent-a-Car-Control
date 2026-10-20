# D01: contrato antigo cadastrado na carga inicial não gera semanas retroativas.

from django.db import migrations, models

CAMPO = dict(
    blank=True,
    null=True,
    help_text="Só para contrato que já rodava antes do sistema: as semanas antes "
    "desta data não são lançadas (já foram acertadas por fora). "
    "Vazio = cobra desde a data de início.",
    verbose_name="cobrar aluguel a partir de",
)


class Migration(migrations.Migration):
    dependencies = [
        ("alocacoes", "0005_passos_entrega"),
    ]

    operations = [
        migrations.AddField(
            model_name="alocacao",
            name="cobrar_a_partir_de",
            field=models.DateField(**CAMPO),
        ),
        migrations.AddField(
            model_name="historicalalocacao",
            name="cobrar_a_partir_de",
            field=models.DateField(**CAMPO),
        ),
    ]
