"""Quem paga hoje e quem está devendo — bloco "Hoje" do Painel e tela "Quem deve".

Responde a pergunta de todo dia (proposta #8, opção C): quem paga hoje, quem
está devendo, quanto e desde quando. Tudo sai de uma consulta só, com o total
quitado anotado em lote (Cobranca.com_quitacao_anotada) e agrupado por
cliente aqui no Python — o número de queries não cresce com as cobranças.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db.models import (
    DecimalField,
    ExpressionWrapper,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
)
from django.db.models.functions import Coalesce
from django.shortcuts import render
from django.utils import timezone

from . import whatsapp
from .models import ZERO, Cobranca, MovimentoCredito

#: Linhas do bloco "Hoje" no Painel; o resto fica em "ver todos".
LINHAS_NO_PAINEL = 5


@dataclass
class Devedor:
    """Um cliente com algo vencido ou vencendo hoje, e o que ele deve."""

    cliente: object
    cobrancas: list = field(default_factory=list)
    atrasado: Decimal = ZERO
    vence_hoje: Decimal = ZERO
    mais_antigo: date | None = None  # vencimento mais antigo em atraso
    judicial: bool = False
    placas: list = field(default_factory=list)
    link_whatsapp: str = ""
    dias_atraso: int = 0
    #: crédito guardado do cliente (sobra de pagamento) — dá para usar nas cobranças
    credito: Decimal = ZERO

    @property
    def total(self):
        return self.atrasado + self.vence_hoje

    @property
    def telefone_discagem(self):
        """Só os dígitos, para o link tel: — "(31) 98888-7777" não disca em todo celular."""
        return "".join(c for c in (self.cliente.telefone or "") if c.isdigit())


def devedores(hoje=None):
    """Clientes com saldo vencido ou vencendo hoje — quem está atrasado primeiro.

    Entra o que ainda é devido (judicial inclusa, mas sem botão de WhatsApp:
    ali a conversa é pelo advogado). Ordem: atraso mais antigo primeiro;
    depois quem só tem a semana vencendo hoje, por nome.
    """
    hoje = hoje or timezone.localdate()
    cobrancas = (
        Cobranca.com_quitacao_anotada(
            Cobranca.objects.filter(
                status__in=Cobranca.STATUS_DEVIDOS, vencimento__lte=hoje
            ).select_related("cliente", "alocacao__veiculo")
        )
        .annotate(credito_do_cliente=_credito_do_cliente())
        .order_by("vencimento", "pk")
    )

    por_cliente = {}
    for cobranca in cobrancas:
        saldo = cobranca.saldo
        if saldo <= ZERO:
            continue
        devedor = por_cliente.get(cobranca.cliente_id)
        if devedor is None:
            devedor = por_cliente[cobranca.cliente_id] = Devedor(cliente=cobranca.cliente)
        # crédito do cliente vem anotado na própria consulta (sem consulta a mais)
        devedor.credito = max(cobranca.credito_do_cliente or ZERO, ZERO)
        devedor.cobrancas.append(cobranca)
        if cobranca.vencimento < hoje:
            devedor.atrasado += saldo
            if devedor.mais_antigo is None:  # já vem ordenado por vencimento
                devedor.mais_antigo = cobranca.vencimento
        else:
            devedor.vence_hoje += saldo
        if cobranca.status == Cobranca.Status.JUDICIAL:
            devedor.judicial = True
        if cobranca.alocacao_id and cobranca.alocacao.veiculo.placa not in devedor.placas:
            devedor.placas.append(cobranca.alocacao.veiculo.placa)

    lista = list(por_cliente.values())
    for devedor in lista:
        if devedor.mais_antigo:
            devedor.dias_atraso = (hoje - devedor.mais_antigo).days
        # a mensagem só leva o que é cobrável (judicial fica de fora)
        devedor.link_whatsapp = whatsapp.link_cliente(devedor.cliente, devedor.cobrancas, hoje)
    lista.sort(
        key=lambda d: (
            d.mais_antigo is None,
            d.mais_antigo or hoje,
            -d.total,
            d.cliente.nome.lower(),
        )
    )
    return lista


def _credito_do_cliente():
    """Subconsulta: crédito guardado (entradas − usos) do cliente da cobrança."""
    dinheiro = DecimalField(max_digits=10, decimal_places=2)

    def soma(tipo):
        return Coalesce(
            Subquery(
                MovimentoCredito.objects.filter(cliente_id=OuterRef("cliente_id"), tipo=tipo)
                .values("cliente_id")
                .annotate(t=Sum("valor"))
                .values("t")
            ),
            Value(ZERO),
            output_field=dinheiro,
        )

    return ExpressionWrapper(
        soma(MovimentoCredito.Tipo.ENTRADA) - soma(MovimentoCredito.Tipo.USO),
        output_field=dinheiro,
    )


def creditos_por_cliente(cliente_ids):
    """{cliente_id: crédito guardado > 0} numa consulta só (entradas − usos)."""
    somas = (
        MovimentoCredito.objects.filter(cliente_id__in=list(cliente_ids))
        .values("cliente_id")
        .annotate(
            entradas=Sum("valor", filter=Q(tipo=MovimentoCredito.Tipo.ENTRADA)),
            usos=Sum("valor", filter=Q(tipo=MovimentoCredito.Tipo.USO)),
        )
    )
    creditos = {}
    for linha in somas:
        saldo = (linha["entradas"] or ZERO) - (linha["usos"] or ZERO)
        if saldo > ZERO:
            creditos[linha["cliente_id"]] = saldo
    return creditos


def vencido_ate_hoje(cliente_ids=None, incluir_judicial=True, hoje=None):
    """{cliente_id: saldo vencido até hoje (inclusive)} — a conta única do "deve".

    É o mesmo número do "Quem deve" e do Receber ("Vencido"): o que vence
    depois de hoje não entra. `incluir_judicial=False` deixa de fora as
    cobranças na Justiça (ex.: o que dá para cobrar pelo WhatsApp).
    Painel e alocações podem reutilizar em vez de refazer a conta.
    """
    hoje = hoje or timezone.localdate()
    status = Cobranca.STATUS_DEVIDOS if incluir_judicial else Cobranca.STATUS_EM_ABERTO
    cobrancas = Cobranca.objects.filter(status__in=status, vencimento__lte=hoje)
    if cliente_ids is not None:
        cobrancas = cobrancas.filter(cliente_id__in=list(cliente_ids))
    totais = {}
    for cobranca in Cobranca.com_quitacao_anotada(cobrancas):
        if cobranca.saldo > ZERO:
            totais[cobranca.cliente_id] = totais.get(cobranca.cliente_id, ZERO) + cobranca.saldo
    return totais


def pagaram_hoje(hoje=None, exceto=()):
    """Nomes de quem tinha cobrança vencendo hoje e já quitou (fica em verde)."""
    hoje = hoje or timezone.localdate()
    return list(
        Cobranca.objects.filter(vencimento=hoje, status=Cobranca.Status.PAGO)
        .exclude(cliente_id__in=list(exceto))
        .order_by("cliente__nome")
        .values_list("cliente__nome", flat=True)
        .distinct()
    )


def resumo_do_dia(hoje=None):
    """Contexto do bloco "Hoje" do Painel: até 5 linhas + totais para o "ver todos"."""
    hoje = hoje or timezone.localdate()
    lista = devedores(hoje)
    return {
        "hoje_linhas": lista[:LINHAS_NO_PAINEL],
        "hoje_mais": max(0, len(lista) - LINHAS_NO_PAINEL),
        "hoje_qtde": len(lista),
        "hoje_total": sum((d.total for d in lista), ZERO),
        "hoje_pagos": pagaram_hoje(hoje, exceto=[d.cliente.pk for d in lista]),
    }


def quem_deve(request):
    """Tela "Quem deve": uma linha por cliente, com total, desde quando e atalhos."""
    hoje = timezone.localdate()
    lista = devedores(hoje)
    return render(
        request,
        "financeiro/quem_deve.html",
        {
            "hoje": hoje,
            "devedores": lista,
            "total_atrasado": sum((d.atrasado for d in lista), ZERO),
            "total_hoje": sum((d.vence_hoje for d in lista), ZERO),
            "total_geral": sum((d.total for d in lista), ZERO),
            "pagos_hoje": pagaram_hoje(hoje, exceto=[d.cliente.pk for d in lista]),
        },
    )
