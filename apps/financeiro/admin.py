from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from simple_history.admin import SimpleHistoryAdmin

from apps.formatos import brl

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


def coluna_em_reais(campo, titulo, ordenar_por=None):
    """Coluna da lista do Admin com o valor em R$ ("R$ 1.234,50", não "1234.50")."""

    @admin.display(description=titulo, ordering=ordenar_por)
    def coluna(self, obj):
        return brl(getattr(obj, campo))

    return coluna


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


class SemIncluirPeloAdmin:
    """Lançamento financeiro não se cria pelo Admin (decisão D11, ETAPA 15).

    Um Recebimento criado aqui aparecia no extrato mas não gerava crédito nem
    entrava no caixa/DAS. O caminho certo são as telas: Receber, Emitir ND e
    Caução — o aviso no topo da lista diz isso.
    """

    tela_certa = ""

    def has_add_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        # Aviso como mensagem (antes ia em "subtitle" e virava o título da aba).
        if self.tela_certa and request.method == "GET":
            messages.info(request, f"Só consulta — para lançar, use {self.tela_certa}")
        return super().changelist_view(request, extra_context)


class SemApagarPeloAdmin:
    """Lançamento financeiro não se apaga pelo Admin (ETAPAS: "Não cobrar", D11).

    Vale para cobrança, ND, recebimento, crédito e caução. Apagar some com o
    registro sem refazer a conta (crédito dado ou tirado sem rastro; caução
    apagada leva junto os descontos que entraram no DAS). O caminho certo: "não
    cobrar" ou cancelar a ND nas cobranças, "↩ desfazer" no recebimento e na
    caução. Sem permissão de apagar, o Django também tira a ação "apagar
    selecionados" da lista.
    """

    def has_delete_permission(self, request, obj=None):
        return False


class InlineSoLeitura:
    """Inline de lançamento financeiro: só mostra — não inclui, não altera, não apaga.

    Apagar uma aplicação reabria a cobrança, mas o dinheiro do recebimento
    sumia do caixa e do DAS (nem crédito, nem cobrança); apagar a sobra que
    virou reforço de caução fazia o mesmo. Para corrigir, "↩ desfazer" nas telas.
    """

    pode_apagar = False

    extra = 0
    campos_financeiros = ()

    def get_readonly_fields(self, request, obj=None):
        return (*super().get_readonly_fields(request, obj), *self.campos_financeiros)

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return self.pode_apagar and super().has_delete_permission(request, obj)


@admin.register(Cobranca)
class CobrancaAdmin(SemApagarPeloAdmin, SemIncluirPeloAdmin, SoLeituraNaEdicao, SimpleHistoryAdmin):
    tela_certa = "Financeiro → Emitir ND (ou o aluguel, que é gerado sozinho)"
    ordering = ["-vencimento"]
    date_hierarchy = "vencimento"
    list_display = ["descricao", "cliente", "origem", "valor_rs", "vencimento", "status"]
    valor_rs = coluna_em_reais("valor", "valor", "valor")
    list_filter = ["status", "origem"]
    search_fields = ["descricao", "cliente__nome"]
    list_select_related = ["cliente"]
    raw_id_fields = ["alocacao", "cliente"]
    campos_financeiros = ("cliente", "origem", "valor", "status", "nota_debito", "cobranca_origem")


class AplicacaoInline(InlineSoLeitura, admin.TabularInline):
    model = AplicacaoRecebimento
    campos_financeiros = ("cobranca", "valor")


@admin.register(Recebimento)
class RecebimentoAdmin(
    SemApagarPeloAdmin, SemIncluirPeloAdmin, SoLeituraNaEdicao, SimpleHistoryAdmin
):
    tela_certa = "Financeiro → Receber"
    ordering = ["-data"]
    list_display = ["cliente", "data", "valor_rs", "forma"]
    valor_rs = coluna_em_reais("valor", "valor", "valor")
    list_select_related = ["cliente"]
    list_filter = ["forma"]
    inlines = [AplicacaoInline]
    campos_financeiros = ("cliente", "valor", "forma")


class ItemNotaDebitoInline(InlineSoLeitura, admin.TabularInline):
    model = ItemNotaDebito
    # apagar um item não refaz o valor da cobrança da ND: cancele a ND (D05)
    campos_financeiros = ("valor", "multa")


@admin.register(NotaDebito)
class NotaDebitoAdmin(
    SemApagarPeloAdmin, SemIncluirPeloAdmin, SoLeituraNaEdicao, SimpleHistoryAdmin
):
    tela_certa = "Financeiro → Emitir ND"
    ordering = ["-data_emissao"]
    list_display = ["numero", "cliente", "data_emissao", "total_rs"]
    total_rs = coluna_em_reais("total", "total")
    list_select_related = ["cliente"]

    def get_queryset(self, request):
        # "total" soma os itens: prefetch evita uma consulta por ND
        return super().get_queryset(request).prefetch_related("itens")

    inlines = [ItemNotaDebitoInline]
    campos_financeiros = ("numero", "cliente")


class MovimentacaoCaucaoInline(InlineSoLeitura, admin.TabularInline):
    model = MovimentacaoCaucao
    campos_financeiros = ("tipo", "valor", "cobranca")


@admin.register(Caucao)
class CaucaoAdmin(SemApagarPeloAdmin, SemIncluirPeloAdmin, SoLeituraNaEdicao, SimpleHistoryAdmin):
    tela_certa = "Financeiro → Cauções"
    list_display = ["alocacao", "recebido", "descontado", "devolvido", "saldo"]
    list_select_related = ["alocacao__veiculo", "alocacao__cliente"]
    inlines = [MovimentacaoCaucaoInline]
    campos_financeiros = ("alocacao",)

    def get_queryset(self, request):
        # recebido/descontado/devolvido anotados em lote (antes: ~12 consultas por linha)
        return Caucao.com_saldos_anotados(super().get_queryset(request))

    recebido = coluna_em_reais("recebido", "recebido", "_recebido_anotado")
    descontado = coluna_em_reais("descontado", "descontado", "_descontado_anotado")
    devolvido = coluna_em_reais("devolvido", "devolvido", "_devolvido_anotado")
    saldo = coluna_em_reais("saldo", "saldo")


@admin.register(MovimentoCredito)
class MovimentoCreditoAdmin(
    SemApagarPeloAdmin, SemIncluirPeloAdmin, SoLeituraNaEdicao, SimpleHistoryAdmin
):
    tela_certa = "Financeiro → Receber (a sobra do pagamento vira crédito)"
    list_display = ["cliente", "tipo", "valor_rs", "data"]
    valor_rs = coluna_em_reais("valor", "valor", "valor")
    list_select_related = ["cliente"]
    list_filter = ["tipo"]
    campos_financeiros = ("cliente", "tipo", "valor")
