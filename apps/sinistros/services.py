"""Regras do sinistro que mexem em outras partes: franquia cobrada do motorista
e roubo do carro (docs.md §4.6)."""

from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.alocacoes.models import Alocacao
from apps.financeiro.models import Cobranca
from apps.financeiro.services import proximo_dia_pagamento
from apps.frota.models import Veiculo

from .models import Sinistro


@transaction.atomic
def cobrar_franquia(sinistro, hoje=None):
    """Cria a cobrança da franquia para o motorista (culpa dele, custo do cliente).

    Mesmo padrão do repasse de manutenção. Vence no próximo dia de pagamento do
    cliente — nunca hoje, senão amanhã ele já vira inadimplente. O registro é
    travado no banco: clique duplo não cria duas cobranças.
    """
    hoje = hoje or date.today()
    sinistro = (
        Sinistro.objects.select_for_update()
        .select_related("veiculo", "cliente")
        .get(pk=sinistro.pk)
    )
    anterior = (
        Cobranca.objects.filter(pk=sinistro.cobranca_franquia_id).only("status").first()
        if sinistro.cobranca_franquia_id
        else None
    )
    if anterior is not None and anterior.status != Cobranca.Status.CANCELADA:
        raise ValidationError("A franquia deste sinistro já foi cobrada do cliente.")
    # cobrança anterior cancelada no financeiro: dá para cobrar de novo (a nova
    # toma o lugar dela no sinistro; a cancelada continua no histórico do cliente)
    if sinistro.responsavel_custo != "cliente":
        raise ValidationError("Quem paga o conserto deste sinistro não é o cliente.")
    if not sinistro.franquia_valor:
        raise ValidationError("Informe o valor da franquia antes de cobrar o cliente.")
    if sinistro.cliente is None:
        raise ValidationError(
            "Nenhum motorista estava com o carro na data — escolha o motorista no sinistro."
        )
    cobranca = Cobranca.objects.create(
        cliente=sinistro.cliente,
        origem=Cobranca.Origem.REPASSE_SINISTRO,
        descricao=(f"Franquia do sinistro {sinistro.veiculo.placa} de {sinistro.data:%d/%m/%Y}"),
        valor=sinistro.franquia_valor,
        vencimento=proximo_dia_pagamento(sinistro.cliente, hoje),
    )
    sinistro.cobranca_franquia = cobranca
    sinistro.save(update_fields=["cobranca_franquia"])
    return cobranca


@transaction.atomic
def registrar_roubo(sinistro):
    """Roubo/furto: encerra a locação na data do roubo e deixa o carro 'Inativo'.

    O aluguel é cobrado até a data do roubo (as semanas seguintes ainda não
    pagas são canceladas pelo encerramento). O carro fica Inativo até alguém
    clicar em "Carro recuperado". Devolve a alocação encerrada, ou None.
    """
    if sinistro.tipo != Sinistro.Tipo.ROUBO:
        return None
    veiculo = Veiculo.objects.select_for_update().get(pk=sinistro.veiculo_id)
    alocacao = (
        Alocacao.objects.filter(veiculo=veiculo, status=Alocacao.Status.ATIVA)
        .order_by("-data_inicio")
        .first()
    )
    if alocacao is not None and alocacao.data_inicio > sinistro.data:
        # a locação de agora começou depois do roubo: é um roubo antigo (o carro já
        # voltou e foi alugado de novo) — nada muda no carro nem na locação
        raise ValidationError(
            f"a locação atual de {alocacao.cliente.nome} começou em "
            f"{alocacao.data_inicio:%d/%m/%Y}, depois da data do roubo. Se o carro foi "
            "roubado de novo, confira a data do sinistro"
        )
    if alocacao is not None:
        km = max(alocacao.km_entrega, veiculo.km_atual or 0)
        alocacao.encerrar(sinistro.data, km)
        veiculo.refresh_from_db()
    if veiculo.status != Veiculo.Status.VENDIDO:
        veiculo.status = Veiculo.Status.INATIVO
        veiculo.save(update_fields=["status"])
    return alocacao


@transaction.atomic
def carro_recuperado(sinistro, data=None):
    """Carro roubado voltou: fica 'Disponível' de novo e a data fica registrada."""
    data = data or date.today()
    sinistro = Sinistro.objects.select_for_update().get(pk=sinistro.pk)
    if sinistro.tipo != Sinistro.Tipo.ROUBO:
        raise ValidationError("Só sinistro de roubo/furto tem carro recuperado.")
    if sinistro.data_recuperacao:
        raise ValidationError(
            f"O carro já foi marcado como recuperado em {sinistro.data_recuperacao:%d/%m/%Y}."
        )
    sinistro.data_recuperacao = data
    sinistro.save(update_fields=["data_recuperacao"])
    veiculo = sinistro.veiculo
    if veiculo.status == Veiculo.Status.INATIVO:
        veiculo.status = Veiculo.Status.DISPONIVEL
        veiculo.save(update_fields=["status"])
    return sinistro
