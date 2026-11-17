"""Etapa 18c — um termo só em todas as telas (não só nas do financeiro).

O dinheiro do cliente que fica com a empresa tem dois nomes, e só dois:
"Caução retida" e "Crédito do cliente". Relatórios, alocações e cadastro de
clientes usam os mesmos nomes que o financeiro — nada de "guardado".
"""

from pathlib import Path

from django.conf import settings

TERMOS_ANTIGOS = ("crédito guardado", "caução guardada", "saldo guardado", "dinheiro guardado")


def test_nenhuma_tela_usa_os_termos_antigos():
    pasta = Path(settings.BASE_DIR) / "templates"
    for arquivo in pasta.rglob("*.html"):
        texto = arquivo.read_text(encoding="utf-8").lower()
        for antigo in TERMOS_ANTIGOS:
            assert antigo not in texto, f"{arquivo.relative_to(pasta)}: “{antigo}”"
