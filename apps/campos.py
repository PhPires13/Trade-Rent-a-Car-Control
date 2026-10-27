"""Entrada de dinheiro do jeito brasileiro — fonte única para formulários e views.

A Luciana digita "1.250,00", "1250,00" ou "R$ 1.250" e o sistema entende;
os campos mostram o valor no mesmo formato ("58.000,00"), nunca "58000.00".
"""

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django import forms
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.utils import timezone

from apps.formatos import numero_br

_MILHAR = re.compile(r"^[1-9]\d{0,2}(\.\d{3})+$")
# "12,000" (vírgula + exatamente 3 dígitos): doze reais ou doze mil? Não dá para saber.
_VIRGULA_AMBIGUA = re.compile(r"^-?\d{1,3},\d{3}$")


def id_valido(texto):
    """pk vinda da URL ou do POST: só algarismos ASCII e de tamanho sensato.

    str.isdigit() aceita "²" e "١٢" (int() recusa) e números gigantes estouram o
    inteiro do banco — os dois virariam erro 500 em vez de serem ignorados.
    """
    return bool(texto) and texto.isascii() and texto.isdigit() and len(texto) <= 9


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


#: Datas fora deste intervalo são quase sempre erro de digitação no celular
#: ("26" virando o ano 0026, "20266" no lugar de 2026).
ANO_MINIMO_DATA = 2000
ANOS_A_FRENTE = 10

#: Texto da opção vazia dos seletores, no lugar do "---------" do Django.
OPCAO_VAZIA = "— escolha —"
_OPCAO_VAZIA_DJANGO = "---------"


def data_plausivel(data):
    """True se o ano da data é razoável (2000 até daqui a 10 anos)."""
    return ANO_MINIMO_DATA <= data.year <= timezone.localdate().year + ANOS_A_FRENTE


#: Formatos de data aceitos em todo formulário: o do calendário do navegador
#: (AAAA-MM-DD) e o digitado à mão (10/05/2026 ou 10/05/26 → 2026).
#: o calendário manda AAAA-MM-DD; quando o navegador mostra campo de texto, a
#: pessoa digita "27/10/2026", "27-10-2026" ou "27.10.2026"
FORMATOS_DE_DATA = ["%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d.%m.%Y"]

#: "27/10" sem ano: completa com o ano corrente
_DIA_E_MES = re.compile(r"^(\d{1,2})[/.-](\d{1,2})$")

MENSAGEM_ANO = "Confira o ano (ex.: 2026)."


def validar_ano_plausivel(data):
    """Recusa data com ano fora de 2000..hoje+10 — "0026" é "26" digitado pela metade."""
    if data is not None and not data_plausivel(data):
        raise ValidationError(MENSAGEM_ANO, code="ano_invalido")


def ler_data(texto, rotulo="Data"):
    """Data do POST cru (fora de formulário Django) com as mesmas regras dos formulários.

    Aceita AAAA-MM-DD, DD/MM/AAAA (também com "-" ou "."), DD/MM/AA e DD/MM
    (ano corrente); recusa ano fora de 2000..hoje+10.
    Erro vira ValidationError com mensagem para a pessoa (nunca erro 500).
    """
    texto = str(texto or "").strip()
    sem_ano = _DIA_E_MES.match(texto)
    if sem_ano:
        texto = f"{sem_ano[1]}/{sem_ano[2]}/{timezone.localdate().year}"
    for formato in FORMATOS_DE_DATA:
        try:
            dia = datetime.strptime(texto, formato).date()
            break
        except ValueError:
            continue
    else:
        raise ValidationError(
            f"{rotulo} inválida — escolha a data no calendário (ex.: 27/10/2026).",
            code="invalid",
        )
    validar_ano_plausivel(dia)
    return dia


def dinheiro_br(texto):
    """Decimal a partir do que a pessoa digitou; InvalidOperation se não der.

    - com vírgula: pontos são milhar e a vírgula é o decimal ("1.250,50");
    - só com ponto(s) em grupos de 3 ("1.850", "12.000"): é milhar;
    - senão o ponto é decimal ("650.00", "0.50");
    - "12,000" e "1,250.00" são ambíguos: ValidationError com a explicação.
    """
    # espaço comum e o não separável (U+00A0) que vem ao copiar "R$ 1.250" de PDFs/sites
    texto = (texto or "").strip().replace("R$", "").replace(" ", "").replace("\u00a0", "")
    if _VIRGULA_AMBIGUA.match(texto) or ("," in texto and "." in texto.split(",", 1)[1]):
        # "12,000" virava R$ 12,00 e "1,250.00" (formato americano) virava R$ 1,25
        raise ValidationError(
            f"Confira o valor: {texto} é ambíguo. Use vírgula só nos centavos "
            "(ex.: 12.000,00 para doze mil ou 12,00 para doze reais).",
            code="valor_ambiguo",
        )
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


def rotulo_veiculo(veiculo):
    """Como o carro aparece nos seletores: placa primeiro ("ABC1D23 — Gol")."""
    return f"{veiculo.placa} — {veiculo.marca_modelo}"


class FormularioBR:
    """Mixin: troca todo DecimalField do formulário por CampoDinheiro.

    Também limita o tamanho de todo campo de arquivo (C17), faz todo campo de
    data virar o calendário do navegador com o valor em AAAA-MM-DD (senão o
    navegador descarta "10/05/2029", a data some na edição e salvar a apaga),
    recusa ano absurdo e troca o "---------" dos seletores por "— escolha —".
    Uso: `class VeiculoForm(FormularioBR, forms.ModelForm)`.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for nome, campo in list(self.fields.items()):
            if isinstance(campo, forms.FileField) and (
                validar_tamanho_arquivo not in campo.validators
            ):
                campo.validators.append(validar_tamanho_arquivo)
            if isinstance(campo, forms.DateField):
                self._ajustar_data(campo)
            self._ajustar_opcao_vazia(campo)
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

    @staticmethod
    def _ajustar_data(campo):
        widget = campo.widget
        # DateTimeInput não herda de DateInput; HiddenInput e outros ficam como estão
        if isinstance(widget, forms.DateInput):
            widget.input_type = "date"
            widget.format = "%Y-%m-%d"
        # não depende do idioma: pt-BR do Django não traz o AAAA-MM-DD do calendário
        campo.input_formats = FORMATOS_DE_DATA
        if validar_ano_plausivel not in campo.validators:
            campo.validators.append(validar_ano_plausivel)

    @staticmethod
    def _ajustar_opcao_vazia(campo):
        if isinstance(campo, forms.ModelChoiceField):
            if campo.empty_label == _OPCAO_VAZIA_DJANGO:
                campo.empty_label = OPCAO_VAZIA
            # carro sempre com a placa primeiro, igual em todo seletor ("ABC1D23 — Gol");
            # o formulário que monta o próprio rótulo (ex.: troca) continua com o dele
            if campo.queryset.model._meta.label == "frota.Veiculo":
                campo.label_from_instance = rotulo_veiculo
            return
        if isinstance(campo, forms.ChoiceField):
            vazia = ("", _OPCAO_VAZIA_DJANGO)
            escolhas = list(campo.choices)
            if vazia in escolhas:
                campo.choices = [("", OPCAO_VAZIA) if e == vazia else e for e in escolhas]
