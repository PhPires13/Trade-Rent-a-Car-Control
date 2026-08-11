"""Agregações dos relatórios mensais para a contabilidade (docs.md §5, decisão nº 18)."""

from calendar import monthrange
from datetime import date

from django.db.models import Q

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
    # frota protegida DO MÊS: comprado até o fim dele e não vendido antes dele —
    # senão os meses passados mudariam a cada compra/venda de carro
    inicio_mes = date(ano, mes, 1)
    fim_mes = date(ano, mes, monthrange(ano, mes)[1])
    frota_protegida = (
        Veiculo.objects.filter(uso=Veiculo.Uso.LOCACAO, mensalidade_protecao__isnull=False)
        .filter(Q(data_aquisicao__isnull=True) | Q(data_aquisicao__lte=fim_mes))
        .exclude(data_venda__lt=inicio_mes)
        .exclude(data_venda__isnull=True, status=Veiculo.Status.VENDIDO)
    )
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


def serie_mensal(ano, mes, quantidade=6):
    """Receita × despesa dos últimos meses (caução fica de fora — não é receita)."""
    pontos = []
    for _ in range(quantidade):
        receitas = receitas_do_mes(ano, mes)
        despesas = despesas_do_mes(ano, mes)
        receita = (
            receitas["locacao"]
            + sum(receitas["diversos"].values(), ZERO)
            + receitas["total_outros_creditos"]
        )
        pontos.append(
            {
                "rotulo": f"{mes:02d}/{ano}",
                "receita": float(receita),
                "despesa": float(despesas["total_geral"]),
            }
        )
        ano, mes = (ano - 1, 12) if mes == 1 else (ano, mes - 1)
    pontos.reverse()
    return pontos


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
