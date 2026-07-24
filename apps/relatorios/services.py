"""Agregações dos relatórios mensais para a contabilidade (docs.md §5, decisão nº 18)."""

from apps.financeiro.models import ZERO, Caucao, Cobranca
from apps.financeiro.services import resumo_fiscal
from apps.frota.desmobilizacao import ranking_da_frota
from apps.frota.models import Veiculo
from apps.manutencao.models import Manutencao
from apps.sinistros.models import AuxilioMotorista, Sinistro

ABERTAS = [
    Cobranca.Status.PENDENTE,
    Cobranca.Status.PARCIAL,
    Cobranca.Status.ATRASADO,
    Cobranca.Status.JUDICIAL,
]


def receitas_do_mes(ano, mes):
    """Créditos do mês nas três classes fiscais (decisão nº 11)."""
    fiscal = resumo_fiscal(ano, mes)
    auxilios = AuxilioMotorista.objects.filter(
        status="recebido", data_recebimento__year=ano, data_recebimento__month=mes
    ).select_related("sinistro__veiculo")
    vendas = Veiculo.objects.filter(data_venda__year=ano, data_venda__month=mes)
    total_auxilios = sum((a.valor or ZERO for a in auxilios), ZERO)
    total_vendas = sum((v.valor_venda or ZERO for v in vendas), ZERO)
    return {
        **fiscal,
        "auxilios": list(auxilios),
        "total_auxilios": total_auxilios,
        "vendas": list(vendas),
        "total_vendas": total_vendas,
        "total_outros_creditos": total_auxilios + total_vendas,
    }


def despesas_do_mes(ano, mes):
    """Despesas do mês por origem — pedido da Luciana para a contabilidade."""
    manutencoes = (
        Manutencao.objects.filter(data__year=ano, data__month=mes, custo_real__isnull=False)
        .select_related("veiculo", "oficina")
        .order_by("data")
    )
    total_manutencao = sum((m.custo_real for m in manutencoes), ZERO)
    frota_protegida = Veiculo.objects.filter(
        uso=Veiculo.Uso.LOCACAO, mensalidade_protecao__isnull=False
    ).exclude(status=Veiculo.Status.VENDIDO)
    total_protecao = sum((v.mensalidade_protecao for v in frota_protegida), ZERO)
    franquias = Sinistro.objects.filter(
        data_evento__year=ano, data_evento__month=mes, franquia_valor__isnull=False
    ).select_related("veiculo")
    total_franquias = sum((s.franquia_valor for s in franquias), ZERO)
    vendas = Veiculo.objects.filter(
        data_venda__year=ano, data_venda__month=mes, custos_venda__isnull=False
    )
    total_custos_venda = sum((v.custos_venda for v in vendas), ZERO)
    return {
        "manutencoes": list(manutencoes),
        "total_manutencao": total_manutencao,
        "frota_protegida": list(frota_protegida),
        "total_protecao": total_protecao,
        "franquias": list(franquias),
        "total_franquias": total_franquias,
        "total_custos_venda": total_custos_venda,
        "total_geral": total_manutencao + total_protecao + total_franquias + total_custos_venda,
    }


def recebiveis_em_aberto():
    """Saldo devedor por cliente (inclui cobrança judicial)."""
    cobrancas = (
        Cobranca.objects.filter(status__in=ABERTAS)
        .select_related("cliente")
        .order_by("cliente__nome", "vencimento")
    )
    por_cliente = {}
    for cobranca in cobrancas:
        saldo = cobranca.saldo
        if saldo <= ZERO:
            continue
        registro = por_cliente.setdefault(
            cobranca.cliente,
            {"cliente": cobranca.cliente, "cobrancas": [], "total": ZERO, "judicial": ZERO},
        )
        registro["cobrancas"].append(cobranca)
        registro["total"] += saldo
        if cobranca.status == Cobranca.Status.JUDICIAL:
            registro["judicial"] += saldo
    return list(por_cliente.values())


def resumo_da_frota(hoje=None):
    fichas, media = ranking_da_frota(hoje)
    return fichas, media


def caucoes_retidas():
    return [c for c in Caucao.objects.select_related("alocacao__cliente") if c.saldo > ZERO]
