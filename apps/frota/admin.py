from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import Categoria, Fornecedor, PagamentoIpva, Veiculo


@admin.register(Fornecedor)
class FornecedorAdmin(SimpleHistoryAdmin):
    list_display = ["nome", "tipo_servico", "contato"]
    search_fields = ["nome"]


@admin.register(Categoria)
class CategoriaAdmin(SimpleHistoryAdmin):
    list_display = ["nome", "valor_semanal_referencia"]


class PagamentoIpvaInline(admin.TabularInline):
    model = PagamentoIpva
    extra = 0


@admin.register(PagamentoIpva)
class PagamentoIpvaAdmin(SimpleHistoryAdmin):
    list_display = ["veiculo", "ano", "parcela", "valor", "vencimento", "pago_em"]
    list_filter = ["ano"]
    search_fields = ["veiculo__placa"]


@admin.register(Veiculo)
class VeiculoAdmin(SimpleHistoryAdmin):
    inlines = [PagamentoIpvaInline]
    list_display = [
        "placa",
        "marca_modelo",
        "ano",
        "categoria",
        "uso",
        "status",
        "km_atual",
        "mensalidade_protecao",
    ]
    list_filter = ["status", "uso", "categoria"]
    search_fields = ["placa", "marca_modelo", "renavam", "chassi"]
