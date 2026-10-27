"""Regras do sinistro que mexem em outras partes: franquia cobrada do motorista
e roubo do carro (docs.md §4.6)."""

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.alocacoes.models import Alocacao, TrocaTemporaria
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
    hoje = hoje or timezone.localdate()
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


@dataclass
class ResultadoRoubo:
    """O que o registro do roubo mudou, para os avisos da tela."""

    #: locação encerrada na data do roubo (o motorista estava com este carro)
    alocacao: Alocacao | None = None
    #: troca encerrada na data do roubo (o carro roubado era o substituto)
    troca: TrocaTemporaria | None = None
    #: locação que continua porque o motorista está com um substituto
    locacao_continua: Alocacao | None = None


@transaction.atomic
def registrar_roubo(sinistro):
    """Roubo/furto: tira o carro do motorista na data do roubo e deixa-o 'Inativo'.

    - Carro roubado era o substituto de uma troca em aberto: a troca termina na
      data do roubo; a locação do motorista continua (o carro dele está na
      oficina) e o dono decide o que fazer com ela.
    - Carro roubado é o da locação e o motorista está com um substituto: a
      locação continua com o substituto (o roubo foi do carro parado).
    - Senão, a locação ativa é encerrada na data do roubo: o aluguel é cobrado
      até ali (as semanas seguintes ainda não pagas são canceladas).

    O carro fica Inativo até alguém clicar em "Carro recuperado". Pode ser
    chamado de novo (botão "Aplicar o roubo"): o que já foi feito não se repete.
    """
    resultado = ResultadoRoubo()
    if sinistro.tipo != Sinistro.Tipo.ROUBO:
        return resultado
    veiculo = Veiculo.objects.select_for_update().get(pk=sinistro.veiculo_id)
    troca = (
        TrocaTemporaria.objects.filter(veiculo_substituto=veiculo, data_devolucao__isnull=True)
        .select_related("alocacao__cliente")
        .first()
    )
    alocacao = (
        Alocacao.objects.filter(veiculo=veiculo, status=Alocacao.Status.ATIVA)
        .order_by("-data_inicio")
        .first()
    )
    inicio = troca.data_retirada if troca else (alocacao.data_inicio if alocacao else None)
    if inicio and inicio > sinistro.data:
        # quem está com o carro hoje pegou-o depois do roubo: é um roubo antigo (o
        # carro já voltou e foi alugado de novo) — nada muda no carro nem na locação
        quem = troca.alocacao.cliente if troca else alocacao.cliente
        raise ValidationError(
            f"{quem.nome} está com o carro desde {inicio:%d/%m/%Y}, depois da data do "
            "roubo. Se o carro foi roubado de novo, confira a data do sinistro"
        )
    if troca is not None:
        troca.devolver(sinistro.data, max(troca.km_retirada, veiculo.km_atual or 0))
        resultado.troca = troca
        veiculo.refresh_from_db()
    elif alocacao is not None and alocacao.troca_ativa is not None:
        resultado.locacao_continua = alocacao
    elif alocacao is not None:
        km = max(alocacao.km_entrega, veiculo.km_atual or 0)
        alocacao.encerrar(sinistro.data, km)
        resultado.alocacao = alocacao
        veiculo.refresh_from_db()
    if veiculo.status != Veiculo.Status.VENDIDO:
        veiculo.status = Veiculo.Status.INATIVO
        veiculo.save(update_fields=["status"])
    return resultado


def _status_sem_roubo(veiculo):
    """Status do carro que deixa de estar roubado (recuperado ou tipo corrigido).

    Em manutenção se tem manutenção sem data de saída; Alocado se ainda é o carro
    de uma locação ativa (o motorista ficou com um substituto) ou o substituto de
    uma troca em aberto; senão Disponível. Assim o carro nunca aparece para alugar
    enquanto ainda está com um motorista.
    """
    from apps.manutencao.services import oficina_por_veiculo  # evita ciclo de import

    if oficina_por_veiculo([veiculo.pk]):
        return Veiculo.Status.EM_MANUTENCAO
    com_motorista = (
        Alocacao.objects.filter(veiculo=veiculo, status=Alocacao.Status.ATIVA).exists()
        or TrocaTemporaria.objects.filter(
            veiculo_substituto=veiculo, data_devolucao__isnull=True
        ).exists()
    )
    return Veiculo.Status.ALOCADO if com_motorista else Veiculo.Status.DISPONIVEL


@transaction.atomic
def deixou_de_ser_roubo(sinistro, veiculo_id=None):
    """O sinistro era roubo e foi corrigido para outro tipo: o carro sai de Inativo.

    Volta a Disponível (ou Em manutenção / Alocado: _status_sem_roubo). Só mexe
    no carro Inativo e sem outro roubo em aberto (não recuperado). A locação ou a
    troca encerradas pelo roubo continuam encerradas — o dono ajusta em Alocações
    se for o caso. `veiculo_id`: o carro de antes, quando a edição trocou o carro
    do sinistro. Devolve True se o carro voltou.
    """
    veiculo = Veiculo.objects.select_for_update().get(pk=veiculo_id or sinistro.veiculo_id)
    if veiculo.status != Veiculo.Status.INATIVO:
        return False
    outro_roubo = (
        Sinistro.objects.filter(
            veiculo=veiculo, tipo=Sinistro.Tipo.ROUBO, data_recuperacao__isnull=True
        )
        .exclude(pk=sinistro.pk)
        .exists()
    )
    if outro_roubo:
        return False
    veiculo.status = _status_sem_roubo(veiculo)
    veiculo.save(update_fields=["status"])
    return True


@transaction.atomic
def carro_recuperado(sinistro, data=None):
    """Carro roubado voltou: sai de Inativo (_status_sem_roubo) e a data fica registrada."""
    data = data or timezone.localdate()
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
        # o roubo do carro parado na oficina não encerrou a locação (o motorista
        # está com um substituto): não pode voltar como Disponível para alugar
        veiculo.status = _status_sem_roubo(veiculo)
        veiculo.save(update_fields=["status"])
    return sinistro
