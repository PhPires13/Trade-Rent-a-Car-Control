"""Entrada de dinheiro do jeito brasileiro — fonte única para formulários e views.

A Luciana digita "1.250,00", "1250,00" ou "R$ 1.250" e o sistema entende;
os campos mostram o valor no mesmo formato ("58.000,00"), nunca "58000.00".
"""

import re
from decimal import Decimal, InvalidOperation

from django import forms
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile

from apps.formatos import numero_br

_MILHAR = re.compile(r"^[1-9]\d{0,2}(\.\d{3})+$")

#: Limite de todo upload (foto, CNH, CRLV, checklist) — um vídeo escolhido por
#: engano no celular não pode ir parar no disco do servidor (C17).
TAMANHO_MAXIMO_UPLOAD = 10 * 1024 * 1024


def validar_tamanho_arquivo(arquivo):
    """Recusa arquivo novo acima de 10 MB; arquivo já gravado não é relido."""
    # FieldFile ainda não gravado (validação do modelo) também é arquivo novo
    novo = isinstance(arquivo, UploadedFile) or getattr(arquivo, "_committed", True) is False
    if novo and arquivo.size > TAMANHO_MAXIMO_UPLOAD:
        raise ValidationError(
            "Arquivo muito grande (máximo 10 MB) — tire uma foto menor ou reexporte o PDF.",
            code="arquivo_grande",
        )


def dinheiro_br(texto):
    """Decimal a partir do que a pessoa digitou; InvalidOperation se não der.

    - com vírgula: pontos são milhar e a vírgula é o decimal ("1.250,50");
    - só com ponto(s) em grupos de 3 ("1.850", "12.000"): é milhar;
    - senão o ponto é decimal ("650.00", "0.50").
    """
    # espaço comum e o não separável (U+00A0) que vem ao copiar "R$ 1.250" de PDFs/sites
    texto = (texto or "").strip().replace("R$", "").replace(" ", "").replace("\u00a0", "")
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    elif _MILHAR.match(texto):
        texto = texto.replace(".", "")
    if not texto:
        raise InvalidOperation("vazio")
    numero = Decimal(texto)
    if not numero.is_finite():  # "nan"/"inf" passariam e quebrariam o banco e as telas
        raise InvalidOperation(texto)
    return numero


def exibir_dinheiro(valor):
    """58000.00 → "58.000,00" (para o value dos inputs)."""
    if valor in (None, ""):
        return ""
    try:
        numero = Decimal(str(valor))
    except (InvalidOperation, ValueError):
        return valor
    if not numero.is_finite():
        return valor
    return f"{'-' if numero < 0 else ''}{numero_br(numero)}"


class CampoDinheiro(forms.DecimalField):
    """DecimalField que aceita e exibe o formato brasileiro.

    Valor negativo é recusado (D28): aluguel, caução, taxa e custos são sempre
    positivos — negativo virava cobrança negativa e abatia dívida de verdade.
    Use `permitir_negativo=True` só onde o negativo tiver sentido.
    """

    def __init__(self, *args, permitir_negativo=False, **kwargs):
        self.permitir_negativo = permitir_negativo
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

    def validate(self, value):
        super().validate(value)
        if not self.permitir_negativo and value is not None and value < 0:
            raise ValidationError("O valor não pode ser negativo.", code="negativo")

    def prepare_value(self, value):
        if isinstance(value, (Decimal, int, float)):
            return exibir_dinheiro(value)
        return value


class FormularioBR:
    """Mixin: troca todo DecimalField do formulário por CampoDinheiro.

    Também limita o tamanho de todo campo de arquivo (C17).
    Uso: `class VeiculoForm(FormularioBR, forms.ModelForm)`.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for nome, campo in list(self.fields.items()):
            if isinstance(campo, forms.FileField) and (
                validar_tamanho_arquivo not in campo.validators
            ):
                campo.validators.append(validar_tamanho_arquivo)
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
