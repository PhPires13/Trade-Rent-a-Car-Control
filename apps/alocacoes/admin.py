from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import Alocacao, TrocaTemporaria


@admin.register(Alocacao)
class AlocacaoAdmin(SimpleHistoryAdmin):
    list_display = [
        "veiculo",
        "cliente",
        "data_inicio",
        "data_termino",
        "valor_semanal",
        "status",
    ]
    list_filter = ["status", "limite_km"]
    search_fields = ["veiculo__placa", "cliente__nome"]


@admin.register(TrocaTemporaria)
class TrocaTemporariaAdmin(SimpleHistoryAdmin):
    list_display = [
        "alocacao",
        "veiculo_substituto",
        "data_retirada",
        "data_devolucao",
        "valor_semanal_ajustado",
    ]
    search_fields = ["veiculo_substituto__placa", "alocacao__cliente__nome"]
