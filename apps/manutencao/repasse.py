"""Repasse de manutenção ao cliente (docs.md §4.5)."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.alocacoes.services import cliente_vigente
from apps.financeiro.models import Cobranca
from apps.financeiro.services import proximo_dia_pagamento


def cliente_do_repasse(manutencao):
    """Quem paga o conserto repassado.

    Ligado a um sinistro: o cliente do sinistro (quem bateu), mesmo que o
    conserto seja feito depois da devolução ou com o carro já com outro
    motorista. Sem sinistro: quem estava com o carro quando ele entrou na
    oficina (ou na data da manutenção, se não houver entrada).
    """
    if manutencao.sinistro_id and manutencao.sinistro.cliente_id:
        return manutencao.sinistro.cliente
    return cliente_vigente(manutencao.veiculo, manutencao.data_entrada or manutencao.data)


@transaction.atomic
def gerar_repasse(manutencao, vencimento=None):
    """Cria a cobrança de repasse quando o custo é do cliente (valor cobrado ≠ custo real).

    Sem vencimento informado, vence no próximo dia de pagamento do cliente
    (mesma regra da ND) — nunca no dia do lançamento.
    """
    if manutencao.responsavel != manutencao.Responsavel.CLIENTE:
        raise ValidationError("O responsável pelo custo não é o cliente.")
    if not manutencao.valor_cobrado_cliente:
        raise ValidationError("Informe o valor cobrado do cliente.")
    if manutencao.pk:
        # trava a linha: dois cliques em "Cobrar do cliente" não geram duas cobranças
        travada = (
            type(manutencao)
            .objects.select_for_update()
            .values_list("cobranca_repasse_id", flat=True)
            .get(pk=manutencao.pk)
        )
        if travada:
            manutencao.cobranca_repasse_id = travada
    if manutencao.cobranca_repasse_id:
        raise ValidationError("Repasse já gerado para esta manutenção.")
    cliente = cliente_do_repasse(manutencao)
    if not cliente:
        raise ValidationError(
            "Nenhum cliente estava com o veículo na data — repasse manual pelo financeiro."
        )
    hoje = timezone.localdate()
    cobranca = Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.REPASSE_MANUTENCAO,
        descricao=f"Repasse manutenção {manutencao.veiculo.placa} — {manutencao.descricao[:80]}",
        valor=manutencao.valor_cobrado_cliente,
        vencimento=vencimento or proximo_dia_pagamento(cliente, hoje),
    )
    manutencao.cobranca_repasse = cobranca
    manutencao.save(update_fields=["cobranca_repasse"])
    return cobranca
