from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from apps.km.admin import coluna_em_km

from .models import IntervaloPersonalizado, ItemPreventiva, Manutencao


@admin.register(ItemPreventiva)
class ItemPreventivaAdmin(SimpleHistoryAdmin):
    list_display = ["nome", "intervalo_fmt", "ativo"]
    intervalo_fmt = coluna_em_km("intervalo_km_padrao", "intervalo padrão (km)")
    list_filter = ["ativo"]


@admin.register(IntervaloPersonalizado)
class IntervaloPersonalizadoAdmin(SimpleHistoryAdmin):
    list_display = ["veiculo", "item", "intervalo_fmt"]
    intervalo_fmt = coluna_em_km("intervalo_km", "intervalo (km)")
    search_fields = ["veiculo__placa"]
    raw_id_fields = ["veiculo"]


@admin.register(Manutencao)
class ManutencaoAdmin(SimpleHistoryAdmin):
    list_display = ["veiculo", "item", "tipo", "data", "km_fmt"]
    km_fmt = coluna_em_km("km", "KM na execução")
    list_filter = ["tipo", "item"]
    search_fields = ["veiculo__placa", "descricao"]
    list_select_related = ["veiculo", "item"]
    # select com todas as cobranças/sinistros fazia centenas de consultas por tela
    raw_id_fields = ["veiculo", "cobranca_repasse", "sinistro"]
