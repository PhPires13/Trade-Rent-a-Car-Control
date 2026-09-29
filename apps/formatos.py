"""Formatação de valores para o usuário — fonte única do "jeito brasileiro".

Registrado como builtin dos templates (settings.TEMPLATES → "builtins"), então
`{{ valor|brl }}` funciona em qualquer template sem `{% load %}`. Do Python,
use `brl(valor)` nas mensagens de tela e erros de validação.
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django import template

register = template.Library()


def brl(valor, vazio="—"):
    """R$ 14.800,00 — milhar com ponto, centavos com vírgula, sempre 2 casas."""
    if valor is None or valor == "":
        return vazio
    try:
        numero = Decimal(str(valor)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return str(valor)
    sinal = "-" if numero < 0 else ""
    inteiro, centavos = f"{abs(numero):.2f}".split(".")
    grupos = []
    while len(inteiro) > 3:
        grupos.insert(0, inteiro[-3:])
        inteiro = inteiro[:-3]
    grupos.insert(0, inteiro)
    return f"{sinal}R$ {'.'.join(grupos)},{centavos}"


def milhar(valor, vazio="—"):
    """48.900 — inteiros (km, pontos) com separador de milhar."""
    if valor is None or valor == "":
        return vazio
    try:
        return f"{int(valor):,}".replace(",", ".")
    except (TypeError, ValueError):
        return str(valor)


register.filter("brl", brl)
register.filter("milhar", milhar)
