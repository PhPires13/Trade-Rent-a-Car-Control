from django.contrib import admin
from django.core.exceptions import PermissionDenied
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

# Decisão D11: valores e status financeiros são só leitura no Admin. Mudar ali
# não refazia a conta (cobrança "Paga" faltando dinheiro, ou dívida que não
# existe). Para corrigir: cancelar/desfazer e lançar de novo pelas telas normais.


class SoLeituraNaEdicao:
    """Trava `campos_financeiros` depois de gravado; na criação continuam livres."""

    campos_financeiros = ()

    def get_readonly_fields(self, request, obj=None):
        travados = self.campos_financeiros if obj is not None else ()
        return (*super().get_readonly_fields(request, obj), *travados)

    # "Reverter" uma versão do histórico regravaria valor/status antigos sem
    # refazer a conta — o mesmo problema da edição. O histórico fica só para ver.
    def revert_disabled(self, request, obj=None):
        return True

    def history_form_view(self, request, object_id, version_id, extra_context=None):
        if request.method == "POST":
            raise PermissionDenied("Lançamento financeiro não se reverte pelo Admin.")
        return super().history_form_view(request, object_id, version_id, extra_context)


class InlineSoLeitura:
    """Inline de lançamento financeiro: mostra, mas não inclui nem altera valores.

    Apagar continua possível onde um signal de post_delete recalcula a cobrança
    (aplicações e movimentações de caução); `pode_apagar = False` trava o resto.
    """

    pode_apagar = True

    extra = 0
    campos_financeiros = ()

    def get_readonly_fields(self, request, obj=None):
        return (*super().get_readonly_fields(request, obj), *self.campos_financeiros)

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return self.pode_apagar and super().has_delete_permission(request, obj)


@admin.register(Cobranca)
class CobrancaAdmin(SoLeituraNaEdicao, SimpleHistoryAdmin):
    list_display = ["descricao", "cliente", "origem", "valor", "vencimento", "status"]
    list_filter = ["status", "origem"]
    search_fields = ["descricao", "cliente__nome"]
    campos_financeiros = ("cliente", "origem", "valor", "status", "nota_debito", "cobranca_origem")


class AplicacaoInline(InlineSoLeitura, admin.TabularInline):
    model = AplicacaoRecebimento
    campos_financeiros = ("cobranca", "valor")


@admin.register(Recebimento)
class RecebimentoAdmin(SoLeituraNaEdicao, SimpleHistoryAdmin):
    list_display = ["cliente", "data", "valor", "forma"]
    list_filter = ["forma"]
    inlines = [AplicacaoInline]
    campos_financeiros = ("cliente", "valor", "forma")


class ItemNotaDebitoInline(InlineSoLeitura, admin.TabularInline):
    model = ItemNotaDebito
    campos_financeiros = ("valor", "multa")
    # apagar um item não refaz o valor da cobrança da ND: cancele a ND (D05)
    pode_apagar = False


@admin.register(NotaDebito)
class NotaDebitoAdmin(SoLeituraNaEdicao, SimpleHistoryAdmin):
    list_display = ["numero", "cliente", "data_emissao", "total"]
    inlines = [ItemNotaDebitoInline]
    campos_financeiros = ("numero", "cliente")


class MovimentacaoCaucaoInline(InlineSoLeitura, admin.TabularInline):
    model = MovimentacaoCaucao
    campos_financeiros = ("tipo", "valor", "cobranca")


@admin.register(Caucao)
class CaucaoAdmin(SoLeituraNaEdicao, SimpleHistoryAdmin):
    list_display = ["alocacao", "recebido", "descontado", "devolvido", "saldo"]
    inlines = [MovimentacaoCaucaoInline]
    campos_financeiros = ("alocacao",)


@admin.register(MovimentoCredito)
class MovimentoCreditoAdmin(SoLeituraNaEdicao, SimpleHistoryAdmin):
    list_display = ["cliente", "tipo", "valor", "data"]
    list_filter = ["tipo"]
    campos_financeiros = ("cliente", "tipo", "valor")
