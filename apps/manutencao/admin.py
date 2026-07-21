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


@admin.register(Manutencao)
class ManutencaoAdmin(SimpleHistoryAdmin):
    list_display = ["veiculo", "item", "tipo", "data", "km", "custo_real", "responsavel"]
    list_filter = ["tipo", "item", "origem_custo", "responsavel", "status_repasse"]
    search_fields = ["veiculo__placa", "descricao"]
