"""Histórico da Retaguarda (django-simple-history) em português e sem erro 500.

Vale para todo Admin que herda de SimpleHistoryAdmin (ligado em config/urls.py):
- as mudanças aparecem do jeito brasileiro: "06/10/2026" e "—" em vez de
  "2026-10-06" e "None", "Alocado" em vez de "alocado", "40.000" em vez de "40000";
- título "Histórico: …" (vinha "View history: …" em inglês);
- registro antigo ligado a um carro (ou cliente) que foi apagado depois não
  derruba mais a tela com erro 500 (o __str__ do modelo lê veiculo.placa).
Os templates em templates/simple_history/ tiram as colunas em inglês.
"""

from datetime import date, datetime
from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist
from django.db import models
from django.utils import timezone
from simple_history.admin import SimpleHistoryAdmin
from simple_history.models import DeletedObject
from simple_history.template_utils import HistoricalRecordContextHelper

from apps.formatos import milhar, numero_br

APAGADO = "(ligado a um cadastro que foi apagado)"


def texto_seguro(obj):
    """str() que não derruba a tela quando o carro/cliente do registro foi apagado."""
    try:
        return str(obj)
    except ObjectDoesNotExist:
        return APAGADO


def valor_em_portugues(campo, valor):
    """Valor gravado → texto para a pessoa ler (None → "—", data → dd/mm/aaaa…)."""
    if valor is None or valor == "":
        return "—"
    if isinstance(valor, DeletedObject):  # ligação vazia ou a um registro apagado
        return (
            "—" if valor.pk is None else f"{valor.model._meta.verbose_name} nº {valor.pk} (apagado)"
        )
    if campo.choices:
        return str(dict(campo.flatchoices).get(valor, valor))
    if isinstance(valor, bool):
        return "Sim" if valor else "Não"
    if isinstance(valor, datetime):
        if timezone.is_aware(valor):
            valor = timezone.localtime(valor)
        return valor.strftime("%d/%m/%Y %H:%M")
    if isinstance(valor, date):
        return valor.strftime("%d/%m/%Y")
    if isinstance(valor, Decimal):
        return f"{'-' if valor < 0 else ''}{numero_br(valor)}"
    if isinstance(valor, int) and not campo.is_relation:
        # ano (2026) fica sem ponto; KM e outros inteiros ganham o milhar
        return str(valor) if "ano" in campo.name.split("_") else milhar(valor)
    if isinstance(campo, models.FileField):
        return getattr(valor, "name", str(valor)) or "—"
    return texto_seguro(valor)


class HistoricoEmPortugues(HistoricalRecordContextHelper):
    def context_for_delta_changes(self, delta):
        # arquivo vazio que passou de NULL para "" aparecia como "— → —"
        return [m for m in super().context_for_delta_changes(delta) if m["old"] != m["new"]]

    def prepare_delta_change_value(self, change, value):
        campo = self.model._meta.get_field(change.field)
        if isinstance(campo, models.ManyToManyField):
            return super().prepare_delta_change_value(change, value)
        return valor_em_portugues(campo, value)


def _ajudante(self, request, historical_record):
    return HistoricoEmPortugues(self.model, historical_record)


def _titulo_da_lista(self, request, obj):
    return f"Histórico: {texto_seguro(obj)}"


def _titulo_da_versao(self, request, obj):
    return f"Versão antiga: {texto_seguro(obj)}"


def ligar():
    SimpleHistoryAdmin.get_historical_record_context_helper = _ajudante
    SimpleHistoryAdmin.history_view_title = _titulo_da_lista
    SimpleHistoryAdmin.history_form_view_title = _titulo_da_versao
