from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import Cliente, CondutorAutorizado


@admin.register(Cliente)
class ClienteAdmin(SimpleHistoryAdmin):
    list_display = ["nome", "cpf_cnpj", "telefone", "status", "cnh_validade", "dia_vencimento"]
    list_filter = ["status"]
    search_fields = ["nome", "cpf_cnpj", "telefone"]


@admin.register(CondutorAutorizado)
class CondutorAutorizadoAdmin(SimpleHistoryAdmin):
    list_display = ["nome", "cpf", "cliente", "contato"]
    search_fields = ["nome", "cpf"]
