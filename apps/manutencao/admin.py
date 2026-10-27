from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import IntervaloPersonalizado, ItemPreventiva, Manutencao


@admin.register(ItemPreventiva)
class ItemPreventivaAdmin(SimpleHistoryAdmin):
    list_display = ["nome", "intervalo_km_padrao", "ativo"]
    list_filter = ["ativo"]


@admin.register(IntervaloPersonalizado)
class IntervaloPersonalizadoAdmin(SimpleHistoryAdmin):
    list_display = ["veiculo", "item", "intervalo_km"]
    search_fields = ["veiculo__placa"]
    raw_id_fields = ["veiculo"]


@admin.register(Manutencao)
class ManutencaoAdmin(SimpleHistoryAdmin):
    list_display = ["veiculo", "item", "tipo", "data", "km"]
    list_filter = ["tipo", "item"]
    search_fields = ["veiculo__placa", "descricao"]
    list_select_related = ["veiculo", "item"]
    # select com todas as cobranças/sinistros fazia centenas de consultas por tela
    raw_id_fields = ["veiculo", "cobranca_repasse", "sinistro"]
