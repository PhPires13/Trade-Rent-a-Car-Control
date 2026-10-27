"""Grava todo CPF/CNPJ no mesmo formato ("529.982.247-25").

Antes o cadastro guardava o texto como foi digitado, e o mesmo CPF com e sem
pontos passava pelo unique e virava dois clientes. Número que não tem 11 nem
14 dígitos fica como está; se dois cadastros já são o mesmo CPF, o segundo
fica como está (o banco não aceitaria dois iguais) para alguém juntar à mão.
"""

import re

from django.db import migrations


def _formatar(texto):
    d = re.sub(r"\D", "", texto or "")
    if len(d) == 11:
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    return texto


def normalizar(apps, schema_editor):
    Cliente = apps.get_model("pessoas", "Cliente")
    usados = set(Cliente.objects.values_list("cpf_cnpj", flat=True))
    for cliente in Cliente.objects.order_by("pk"):
        novo = _formatar(cliente.cpf_cnpj)
        if novo == cliente.cpf_cnpj or novo in usados:
            continue
        usados.discard(cliente.cpf_cnpj)
        usados.add(novo)
        # update direto: não cria versão no histórico só por causa do formato
        Cliente.objects.filter(pk=cliente.pk).update(cpf_cnpj=novo)


class Migration(migrations.Migration):
    dependencies = [
        ("pessoas", "0003_alter_cliente_cnh_frente_alter_cliente_cnh_verso_and_more"),
    ]

    operations = [migrations.RunPython(normalizar, migrations.RunPython.noop)]
