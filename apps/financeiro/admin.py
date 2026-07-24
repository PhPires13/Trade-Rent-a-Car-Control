from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import (
    AplicacaoRecebimento,
    Caucao,
    Cobranca,
    ItemNotaDebito,
    MovimentacaoCaucao,
    MovimentoCredito,
    NotaDebito,
    Recebimento,
)


@admin.register(Cobranca)
class CobrancaAdmin(SimpleHistoryAdmin):
    list_display = ["descricao", "cliente", "origem", "valor", "vencimento", "status"]
    list_filter = ["status", "origem"]
    search_fields = ["descricao", "cliente__nome"]


class AplicacaoInline(admin.TabularInline):
    model = AplicacaoRecebimento
    extra = 0


@admin.register(Recebimento)
class RecebimentoAdmin(SimpleHistoryAdmin):
    list_display = ["cliente", "data", "valor", "forma"]
    list_filter = ["forma"]
    inlines = [AplicacaoInline]


class ItemNotaDebitoInline(admin.TabularInline):
    model = ItemNotaDebito
    extra = 0


@admin.register(NotaDebito)
class NotaDebitoAdmin(SimpleHistoryAdmin):
    list_display = ["numero", "cliente", "data_emissao", "total"]
    inlines = [ItemNotaDebitoInline]


class MovimentacaoCaucaoInline(admin.TabularInline):
    model = MovimentacaoCaucao
    extra = 0


@admin.register(Caucao)
class CaucaoAdmin(SimpleHistoryAdmin):
    list_display = ["alocacao", "recebido", "descontado", "devolvido", "saldo"]
    inlines = [MovimentacaoCaucaoInline]


@admin.register(MovimentoCredito)
class MovimentoCreditoAdmin(SimpleHistoryAdmin):
    list_display = ["cliente", "tipo", "valor", "data"]
    list_filter = ["tipo"]
