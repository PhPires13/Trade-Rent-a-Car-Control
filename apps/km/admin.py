from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import AcertoExcedenteKm, RegistroKm


@admin.register(RegistroKm)
class RegistroKmAdmin(SimpleHistoryAdmin):
    list_display = ["veiculo", "mes_referencia", "data_leitura", "km", "km_anterior", "dias"]
    list_filter = ["mes_referencia"]
    search_fields = ["veiculo__placa"]


@admin.register(AcertoExcedenteKm)
class AcertoExcedenteKmAdmin(admin.ModelAdmin):
    """Só consulta: o acerto nasce e some junto com a leitura e a cobrança.

    Mudar à mão o contrato ou a cobrança daqui desligaria o excedente de quem
    usou o carro — para não cobrar, cancele a cobrança no Financeiro.
    """

    list_display = ["registro", "alocacao", "cobranca"]
    list_filter = ["registro__mes_referencia"]
    search_fields = ["registro__veiculo__placa", "alocacao__cliente__nome"]
    list_select_related = ["registro__veiculo", "alocacao__cliente", "cobranca"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
