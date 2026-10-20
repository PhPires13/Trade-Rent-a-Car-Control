# D04/D12: período de aluguel pago por cada cobrança (semanas proporcionais).
# Cobranças antigas ficam vazias = a semana a partir do vencimento (sem mudança).

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("financeiro", "0002_itemnotadebito_multa"),
    ]

    operations = [
        migrations.AddField(
            model_name=modelo,
            name=nome,
            field=models.DateField(blank=True, null=True, verbose_name=rotulo),
        )
        for modelo in ("cobranca", "historicalcobranca")
        for nome, rotulo in (("periodo_inicio", "aluguel de"), ("periodo_fim", "aluguel até"))
    ]
