import re
from datetime import date

from django import forms
from django.core.exceptions import ValidationError

from apps.formatos import milhar

from .models import DIAS_DE_FECHAMENTO, RegistroKm, leitura_cabe_no_mes

CLASSE_CAMPO = "w-full rounded border border-gray-300 px-3 py-3 text-lg sm:py-2 sm:text-base"

#: Nenhum carro de aplicativo chega perto disso — acima, é número digitado errado
#: (e evita estourar a coluna inteira do banco com um erro 500).
KM_MAXIMO = 1_999_999
#: "48900", "48.900" ou "48 900" — ponto/espaço só como separador de milhar.
_KM = re.compile(r"^\d{1,3}([. ]\d{3})+$|^\d+$")


def km_digitado(texto):
    """KM digitado → inteiro; ValidationError com texto simples se não der.

    "12.5" NÃO vira 125: ponto fora do lugar de milhar é recusado, para um
    número torto não passar como outro.
    """
    texto = (texto or "").strip().replace(" ", " ")
    if not _KM.match(texto):
        raise ValidationError("Digite o KM só com números (ex.: 48900 ou 48.900).")
    km = int(texto.replace(".", "").replace(" ", ""))
    if km > KM_MAXIMO:
        raise ValidationError(f"{milhar(km)} km é grande demais — confira o número do painel.")
    return km


def data_nao_futura(data, hoje=None):
    """Leitura é do odômetro que alguém olhou — não pode ser de um dia que ainda não chegou."""
    if data > (hoje or date.today()):
        raise ValidationError(
            f"A data {data:%d/%m/%Y} ainda não chegou — use a data em que olhou o painel."
        )
    return data


class CampoKm(forms.IntegerField):
    """KM aceitando ponto de milhar; o campo é texto com teclado numérico no celular."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault(
            "widget",
            forms.TextInput(
                attrs={"inputmode": "numeric", "autocomplete": "off", "class": CLASSE_CAMPO}
            ),
        )
        super().__init__(*args, **kwargs)

    def to_python(self, value):
        if value in self.empty_values:
            return None
        if isinstance(value, str):
            return km_digitado(value)
        return super().to_python(value)


class CorrecaoLeituraForm(forms.ModelForm):
    """Correção de uma leitura de KM gravada errada (dentro do mesmo mês)."""

    km = CampoKm(label="KM do odômetro")
    confirmo = forms.BooleanField(label="Sim, o número está certo", required=False)

    class Meta:
        model = RegistroKm
        fields = ["data_leitura", "km", "observacoes"]
        widgets = {
            "data_leitura": forms.DateInput(
                format="%Y-%m-%d", attrs={"type": "date", "class": CLASSE_CAMPO}
            ),
            "observacoes": forms.Textarea(attrs={"rows": 2, "class": CLASSE_CAMPO}),
        }

    def clean_data_leitura(self):
        data = data_nao_futura(self.cleaned_data["data_leitura"])
        if not leitura_cabe_no_mes(data, self.instance.mes_referencia):
            raise forms.ValidationError(
                f"A data precisa ficar em {self.instance.mes_referencia:%m/%Y} (ou nos "
                f"{DIAS_DE_FECHAMENTO} primeiros dias do mês seguinte, para fechar o mês). "
                f"Para mudar o mês, apague esta leitura e registre de novo no mês certo."
            )
        return data

    def confirmou(self):
        """A caixinha só vale para o número que foi mostrado no aviso.

        Se a pessoa trocar o KM depois do aviso e deixar a caixinha marcada,
        o número novo passa pela conferência de novo.
        """
        return self.data.get("confirmo") == str(self.cleaned_data.get("km"))
