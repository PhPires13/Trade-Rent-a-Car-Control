"""Relatórios financeiros e base do DAS (docs.md §5)."""

from datetime import date
from decimal import Decimal

from django.db.models import Sum, Value
from django.db.models.functions import Coalesce

from .models import BaixaCobranca, Cobranca


def _mes_intervalo(mes):
    inicio = mes.replace(day=1)
    if inicio.month == 12:
        fim = inicio.replace(year=inicio.year + 1, month=1)
    else:
        fim = inicio.replace(month=inicio.month + 1)
    return inicio, fim


def base_das_do_mes(mes):
    """Classifica os recebimentos do mês em receita de locação × pagamentos diversos.

    A base do DAS é só a receita de locação (docs.md §4.3, decisão nº 11).
    Calculado sobre o que foi efetivamente recebido no mês.
    """
    inicio, fim = _mes_intervalo(mes)
    baixas = BaixaCobranca.objects.filter(
        recebimento__data__gte=inicio, recebimento__data__lt=fim
    ).select_related("cobranca")

    receita_locacao = Decimal("0")
    pagamentos_diversos = Decimal("0")
    for baixa in baixas:
        if baixa.cobranca.origem in Cobranca.ORIGENS_RECEITA_LOCACAO:
            receita_locacao += baixa.valor
        else:
            pagamentos_diversos += baixa.valor

    return {
        "mes": inicio,
        "receita_locacao": receita_locacao,
        "pagamentos_diversos": pagamentos_diversos,
        "base_das": receita_locacao,
        "total_recebido": receita_locacao + pagamentos_diversos,
    }


def recebiveis_por_cliente():
    """Saldo em aberto por cliente, agrupado (docs.md §5)."""
    abertas = (
        Cobranca.objects.exclude(status=Cobranca.Status.PAGO)
        .select_related("cliente")
        .order_by("cliente__nome", "vencimento")
    )
    por_cliente = {}
    hoje = date.today()
    for cobranca in abertas:
        dados = por_cliente.setdefault(
            cobranca.cliente,
            {"total": Decimal("0"), "atrasado": Decimal("0"), "judicial": Decimal("0")},
        )
        saldo = cobranca.saldo
        dados["total"] += saldo
        if cobranca.status == Cobranca.Status.JUDICIAL:
            dados["judicial"] += saldo
        elif cobranca.vencimento < hoje:
            dados["atrasado"] += saldo
    return por_cliente


def total_a_receber():
    """Soma dos saldos em aberto (valor − baixas)."""
    resultado = Cobranca.objects.exclude(status=Cobranca.Status.PAGO).aggregate(
        valor=Coalesce(Sum("valor"), Value(Decimal("0"))),
        pago=Coalesce(Sum("baixas__valor"), Value(Decimal("0"))),
    )
    return resultado["valor"] - resultado["pago"]
