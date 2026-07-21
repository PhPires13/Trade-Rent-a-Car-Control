from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import (
    BaixaCobranca,
    Caucao,
    Cobranca,
    ItemNotaDebito,
    MovimentacaoCaucao,
    NotaDebito,
    Recebimento,
)


@admin.register(Cobranca)
class CobrancaAdmin(SimpleHistoryAdmin):
    list_display = ["cliente", "origem", "valor", "vencimento", "status"]
    list_filter = ["origem", "status"]
    search_fields = ["cliente__nome", "descricao"]


class BaixaInline(admin.TabularInline):
    model = BaixaCobranca
    extra = 0


@admin.register(Recebimento)
class RecebimentoAdmin(SimpleHistoryAdmin):
    list_display = ["cliente", "valor", "data", "forma"]
    list_filter = ["forma"]
    search_fields = ["cliente__nome"]
    inlines = [BaixaInline]


class ItemNotaDebitoInline(admin.TabularInline):
    model = ItemNotaDebito
    extra = 1


@admin.register(NotaDebito)
class NotaDebitoAdmin(SimpleHistoryAdmin):
    list_display = ["numero", "cliente", "data_emissao", "status"]
    list_filter = ["status"]
    search_fields = ["cliente__nome", "numero"]
    inlines = [ItemNotaDebitoInline]


class MovimentacaoCaucaoInline(admin.TabularInline):
    model = MovimentacaoCaucao
    extra = 0


@admin.register(Caucao)
class CaucaoAdmin(SimpleHistoryAdmin):
    list_display = ["cliente", "status"]
    list_filter = ["status"]
    search_fields = ["cliente__nome"]
    inlines = [MovimentacaoCaucaoInline]
