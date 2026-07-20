from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import RegistroKm


@admin.register(RegistroKm)
class RegistroKmAdmin(SimpleHistoryAdmin):
    list_display = ["veiculo", "mes_referencia", "data_leitura", "km", "km_anterior", "dias"]
    list_filter = ["mes_referencia"]
    search_fields = ["veiculo__placa"]
