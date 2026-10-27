from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import AuxilioMotorista, Sinistro


class AuxilioInline(admin.TabularInline):
    model = AuxilioMotorista
    extra = 0


@admin.register(Sinistro)
class SinistroAdmin(SimpleHistoryAdmin):
    list_display = ["veiculo", "data", "cliente", "tipo", "envolvido", "status"]
    list_filter = ["tipo", "envolvido", "status"]
    search_fields = ["veiculo__placa", "cliente__nome"]
    list_select_related = ["veiculo", "cliente"]
    # select com todas as cobranças/clientes do sistema deixava a página pesada
    raw_id_fields = ["cobranca_franquia", "cliente"]
    inlines = [AuxilioInline]


@admin.register(AuxilioMotorista)
class AuxilioMotoristaAdmin(SimpleHistoryAdmin):
    list_display = ["sinistro", "valor", "status", "data_recebimento"]
    list_filter = ["status"]
    list_select_related = ["sinistro__veiculo"]
