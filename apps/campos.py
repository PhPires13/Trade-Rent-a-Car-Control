"""Entrada de dinheiro do jeito brasileiro — fonte única para formulários e views.

A Luciana digita "1.250,00", "1250,00" ou "R$ 1.250" e o sistema entende;
os campos mostram o valor no mesmo formato ("58.000,00"), nunca "58000.00".
"""

import re
from decimal import Decimal, InvalidOperation

from django import forms
from django.core.exceptions import ValidationError

_MILHAR = re.compile(r"^[1-9]\d{0,2}(\.\d{3})+$")


def dinheiro_br(texto):
    """Decimal a partir do que a pessoa digitou; InvalidOperation se não der.

    - com vírgula: pontos são milhar e a vírgula é o decimal ("1.250,50");
    - só com ponto(s) em grupos de 3 ("1.850", "12.000"): é milhar;
    - senão o ponto é decimal ("650.00", "0.50").
    """
    texto = (texto or "").strip().replace("R$", "").replace(" ", "").replace(" ", "")
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    elif _MILHAR.match(texto):
        texto = texto.replace(".", "")
    if not texto:
        raise InvalidOperation("vazio")
    return Decimal(texto)


def exibir_dinheiro(valor):
    """58000.00 → "58.000,00" (para o value dos inputs)."""
    if valor in (None, ""):
        return ""
    try:
        numero = Decimal(str(valor))
    except (InvalidOperation, ValueError):
        return valor
    inteiro, _, centavos = f"{abs(numero):.2f}".partition(".")
    grupos = []
    while len(inteiro) > 3:
        grupos.insert(0, inteiro[-3:])
        inteiro = inteiro[:-3]
    grupos.insert(0, inteiro)
    return f"{'-' if numero < 0 else ''}{'.'.join(grupos)},{centavos}"


class CampoDinheiro(forms.DecimalField):
    """DecimalField que aceita e exibe o formato brasileiro."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault(
            "widget",
            forms.TextInput(attrs={"inputmode": "decimal", "placeholder": "0,00"}),
        )
        super().__init__(*args, **kwargs)

    def to_python(self, value):
        if value in self.empty_values:
            return None
        if isinstance(value, str):
            try:
                value = dinheiro_br(value)
            except (InvalidOperation, ValueError):
                raise ValidationError(
                    "Valor inválido — use só números, com vírgula nos centavos (ex.: 1.250,00).",
                    code="invalid",
                ) from None
        return super().to_python(value)

    def prepare_value(self, value):
        if isinstance(value, (Decimal, int, float)):
            return exibir_dinheiro(value)
        return value


class FormularioBR:
    """Mixin: troca todo DecimalField do formulário por CampoDinheiro.

    Uso: `class VeiculoForm(FormularioBR, forms.ModelForm)`.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for nome, campo in list(self.fields.items()):
            if isinstance(campo, forms.DecimalField) and not isinstance(campo, CampoDinheiro):
                novo = CampoDinheiro(
                    required=campo.required,
                    label=campo.label,
                    help_text=campo.help_text,
                    initial=campo.initial,
                    max_digits=campo.max_digits,
                    decimal_places=campo.decimal_places,
                    disabled=campo.disabled,
                )
                # os validadores do campo original já incluem o de casas decimais
                novo.validators = list(campo.validators)
                self.fields[nome] = novo
