"""Formatação de valores para o usuário — fonte única do "jeito brasileiro".

Registrado como builtin dos templates (settings.TEMPLATES → "builtins"), então
`{{ valor|brl }}` funciona em qualquer template sem `{% load %}`. Do Python,
use `brl(valor)` nas mensagens de tela e erros de validação.
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django import template

register = template.Library()

_PONTO_POR_VIRGULA = str.maketrans(",.", ".,")


def numero_br(numero):
    """Decimal → "1.234.567,50": milhar com ponto, 2 casas com vírgula, sem sinal.

    Fonte única do agrupamento — usada por `brl` e pelo valor dos campos de
    dinheiro (apps/campos.py). Quem chama decide o arredondamento e o sinal.
    """
    return f"{abs(numero):,.2f}".translate(_PONTO_POR_VIRGULA)


def brl(valor, vazio="—"):
    """R$ 14.800,00 — milhar com ponto, centavos com vírgula, sempre 2 casas."""
    if valor is None or valor == "":
        return vazio
    try:
        numero = Decimal(str(valor)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return str(valor)
    if not numero.is_finite():  # NaN: o filtro nunca pode derrubar a página
        return str(valor)
    return f"{'-' if numero < 0 else ''}R$ {numero_br(numero)}"


def milhar(valor, vazio="—"):
    """48.900 — inteiros (km, pontos) com separador de milhar."""
    if valor is None or valor == "":
        return vazio
    try:
        return f"{int(valor):,}".replace(",", ".")
    except (TypeError, ValueError, OverflowError):  # OverflowError: int(infinito)
        return str(valor)


register.filter("brl", brl)
register.filter("milhar", milhar)
