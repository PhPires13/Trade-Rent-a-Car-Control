from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import AuxilioMotorista, Sinistro


class AuxilioInline(admin.TabularInline):
    model = AuxilioMotorista
    extra = 0


@admin.register(Sinistro)
class SinistroAdmin(SimpleHistoryAdmin):
    list_display = ["data", "veiculo", "motorista", "tipo", "envolvido", "status"]
    list_filter = ["tipo", "envolvido", "status", "acionou_protecao"]
    search_fields = ["veiculo__placa", "motorista__nome"]
    inlines = [AuxilioInline]


@admin.register(AuxilioMotorista)
class AuxilioMotoristaAdmin(SimpleHistoryAdmin):
    list_display = ["sinistro", "dias_parado", "valor", "status", "data_recebimento"]
    list_filter = ["status"]
