"""Regras de negócio do financeiro (docs.md §4.3–4.4).

Decisões dos donos aplicadas aqui:
- nº 7: cobrança semanal no dia de vencimento de cada cliente
- nº 13: encargos 5% (até ~4 dias) / 10% (acima), ajustáveis caso a caso
- nº 14: inadimplente a partir de 1 dia de atraso
- nº 11: DAS calculado só sobre a receita de locação
"""

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import OuterRef, Q, Subquery, Sum
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.formatos import brl
from apps.pessoas.models import Cliente

from .models import (
    ZERO,
    AplicacaoRecebimento,
    Caucao,
    Cobranca,
    ItemNotaDebito,
    MovimentacaoCaucao,
    MovimentoCredito,
    NotaDebito,
    Recebimento,
)

ENCARGO_CURTO = Decimal("0.05")
ENCARGO_LONGO = Decimal("0.10")
ENCARGO_DIAS_CORTE = 4
DIAS_PARA_INADIMPLENCIA = 1
#: Mesmo cliente + valor + data dentro desta janela = toque duplo no Confirmar.
JANELA_RECEBIMENTO_REPETIDO = timedelta(minutes=2)


class RecebimentoRepetido(ValidationError):
    """O mesmo pagamento acabou de ser lançado (toque duplo, 4G lento)."""

    def __init__(self, recebimento):
        super().__init__(
            "Este pagamento já foi lançado agora há pouco — não foi lançado de novo. "
            "Se for mesmo um segundo pagamento igual, espere 2 minutos e lance outra vez."
        )
        self.recebimento = recebimento


def _proximo_vencimento(alocacao):
    """Vencimento da próxima cobrança de aluguel a gerar.

    Ancorado na última cobrança existente (inclusive cancelada), para que uma
    mudança de dia_vencimento em contrato ativo valha só para frente — sem
    regenerar semanas já cobradas nem semanas canceladas.
    """
    ultima = (
        Cobranca.objects.filter(alocacao=alocacao, origem=Cobranca.Origem.ALUGUEL)
        .order_by("-vencimento")
        .first()
    )
    data = ultima.vencimento + timedelta(days=1) if ultima else alocacao.data_inicio
    while data.weekday() != alocacao.dia_vencimento:
        data += timedelta(days=1)
    return data


def _valor_da_semana(alocacao, vencimento):
    """Valor da semana [vencimento, vencimento+6], rateado por dia (docs.md §4.2).

    Troca temporária com valor ajustado vale pelos dias em que está em curso;
    o restante da semana sai pelo valor da alocação (decisão nº 1).
    """
    fim_da_semana = vencimento + timedelta(days=6)
    trocas = list(
        alocacao.trocas.filter(
            valor_semanal_ajustado__isnull=False, data_retirada__lte=fim_da_semana
        ).filter(Q(data_devolucao__isnull=True) | Q(data_devolucao__gte=vencimento))
    )
    if not trocas:
        return alocacao.valor_semanal
    total = ZERO
    for i in range(7):
        dia = vencimento + timedelta(days=i)
        vigente = next(
            (
                troca.valor_semanal_ajustado
                for troca in trocas
                if troca.data_retirada <= dia
                and (troca.data_devolucao is None or troca.data_devolucao >= dia)
            ),
            alocacao.valor_semanal,
        )
        total += vigente
    return (total / 7).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def gerar_cobrancas_semanais(hoje=None):
    """Gera as cobranças de aluguel vencidas até hoje (idempotente; recupera dias perdidos)."""
    hoje = hoje or date.today()
    criadas = []
    alocacoes = Alocacao.objects.filter(
        Q(status=Alocacao.Status.ATIVA) | Q(data_termino__gte=hoje - timedelta(days=60))
    ).select_related("cliente", "veiculo")
    for alocacao in alocacoes:
        vencimento = _proximo_vencimento(alocacao)
        # Cobrança pré-paga: a semana só é devida se começa ANTES da devolução.
        if alocacao.data_termino:
            limite = min(hoje, alocacao.data_termino - timedelta(days=1))
        else:
            limite = hoje
        while vencimento <= limite:
            cobranca, criada = Cobranca.objects.get_or_create(
                alocacao=alocacao,
                vencimento=vencimento,
                origem=Cobranca.Origem.ALUGUEL,
                defaults={
                    "cliente": alocacao.cliente,
                    "descricao": f"Aluguel semanal {alocacao.veiculo.placa} "
                    f"— venc. {vencimento:%d/%m/%Y}",
                    "valor": _valor_da_semana(alocacao, vencimento),
                },
            )
            if criada:
                criadas.append(cobranca)
            vencimento += timedelta(days=7)
    return criadas


def _filtro_devedor(limite):
    """Cobrança que torna o cliente devedor: atrasada há >= 1 dia ou judicial (nº 14 e 17)."""
    return Q(status=Cobranca.Status.ATRASADO, vencimento__lte=limite) | Q(
        status=Cobranca.Status.JUDICIAL
    )


def marcar_atrasos(hoje=None):
    """Atualiza cobranças atrasadas e a inadimplência dos clientes (decisões nº 14 e 17)."""
    hoje = hoje or date.today()
    limite = hoje - timedelta(days=DIAS_PARA_INADIMPLENCIA)
    abertas = Cobranca.objects.filter(status__in=Cobranca.STATUS_EM_ABERTO)
    for cobranca in abertas:
        cobranca.atualizar_status(hoje)
    devedores = set(
        Cobranca.objects.filter(_filtro_devedor(limite)).values_list("cliente_id", flat=True)
    )
    Cliente.objects.filter(pk__in=devedores).exclude(status=Cliente.Status.INATIVO).update(
        status=Cliente.Status.INADIMPLENTE
    )
    Cliente.objects.filter(status=Cliente.Status.INADIMPLENTE).exclude(pk__in=devedores).update(
        status=Cliente.Status.ATIVO
    )


def atualizar_inadimplencia(cliente, hoje=None):
    """Recalcula o status do cliente na hora do pagamento (decisões nº 14 e 17).

    Quem quita as pendências volta a Ativo sem esperar a rotina do dia
    seguinte; cobrança judicial mantém o devedor Inadimplente; Inativo
    nunca é alterado.
    """
    hoje = hoje or date.today()
    cliente.refresh_from_db(fields=["status"])  # o cron pode ter mudado após o objeto carregar
    if cliente.status == Cliente.Status.INATIVO:
        return
    limite = hoje - timedelta(days=DIAS_PARA_INADIMPLENCIA)
    devedor = cliente.cobrancas.filter(_filtro_devedor(limite)).exists()
    if devedor:
        novo = Cliente.Status.INADIMPLENTE
    elif cliente.status == Cliente.Status.INADIMPLENTE:
        novo = Cliente.Status.ATIVO
    else:
        return
    if cliente.status != novo:
        cliente.status = novo
        cliente.save(update_fields=["status"])


def sugerir_encargo(cobranca, hoje=None):
    """5% até ~4 dias de atraso, 10% acima — sempre ajustável pelo dono (decisão nº 13)."""
    hoje = hoje or date.today()
    dias = (hoje - cobranca.vencimento).days
    if dias <= 0 or cobranca.saldo <= ZERO:
        return ZERO
    taxa = ENCARGO_CURTO if dias <= ENCARGO_DIAS_CORTE else ENCARGO_LONGO
    return (cobranca.saldo * taxa).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@transaction.atomic
def aplicar_encargo(cobranca, valor, hoje=None):
    """Cria a cobrança de encargo vinculada à atrasada (fora da base do DAS)."""
    hoje = hoje or date.today()
    if valor <= ZERO:
        raise ValidationError("O valor do encargo deve ser maior que zero.")
    if cobranca.encargos.exclude(status=Cobranca.Status.CANCELADA).exists():
        raise ValidationError("Esta cobrança já tem encargo aplicado.")
    return Cobranca.objects.create(
        cliente=cobranca.cliente,
        alocacao=cobranca.alocacao,
        origem=Cobranca.Origem.ENCARGO,
        descricao=f"Encargo por atraso — {cobranca.descricao}",
        valor=valor,
        vencimento=hoje,
        cobranca_origem=cobranca,
    )


@transaction.atomic
def registrar_recebimento(
    cliente,
    data,
    valor,
    forma,
    aplicacoes,
    sobra_destino="credito",
    observacoes="",
    bloquear_repetido=False,
):
    """Lança um pagamento e distribui nas cobranças, com travas (docs.md §4.3).

    aplicacoes: lista de (cobranca, valor). Travas: nenhuma aplicação acima do
    saldo da cobrança; total aplicado nunca acima do recebido. Sobra vira
    crédito do cliente ou reforço de caução.

    bloquear_repetido (tela de receber): o mesmo cliente + valor + data lançado
    há menos de 2 minutos levanta RecebimentoRepetido — toque duplo no
    Confirmar com internet lenta não vira pagamento em dobro.
    """
    valor = Decimal(valor)
    if valor <= ZERO:
        raise ValidationError("Valor recebido deve ser maior que zero.")
    if forma not in Recebimento.Forma.values:
        raise ValidationError('Escolha em "Como pagou" a forma do pagamento.')
    if bloquear_repetido:
        # trava a linha do cliente: dois Confirmar simultâneos esperam um pelo outro
        Cliente.objects.select_for_update().filter(pk=cliente.pk).first()
        repetido = _recebimento_recente(cliente, data, valor)
        if repetido:
            raise RecebimentoRepetido(repetido)
    total = sum((Decimal(v) for _, v in aplicacoes), ZERO)
    if total > valor:
        raise ValidationError(
            f"Total alocado ({brl(total)}) maior que o valor recebido ({brl(valor)})."
        )
    if forma == Recebimento.Forma.CREDITO:
        saldo_credito = MovimentoCredito.saldo_do_cliente(cliente)
        if valor > saldo_credito:
            raise ValidationError(f"Crédito insuficiente (saldo {brl(saldo_credito)}).")
    recebimento = Recebimento.objects.create(
        cliente=cliente, data=data, valor=valor, forma=forma, observacoes=observacoes
    )
    if forma == Recebimento.Forma.CREDITO:
        MovimentoCredito.objects.create(
            cliente=cliente,
            tipo=MovimentoCredito.Tipo.USO,
            valor=valor,
            data=data,
            recebimento=recebimento,
            observacoes="Uso de crédito em recebimento",
        )
    for cobranca, valor_aplicado in aplicacoes:
        valor_aplicado = Decimal(valor_aplicado)
        if valor_aplicado <= ZERO:
            continue
        if cobranca.cliente_id != cliente.pk:
            raise ValidationError(f"Cobrança {cobranca} não é deste cliente.")
        if valor_aplicado > cobranca.saldo:
            raise ValidationError(
                f"Aplicação de {brl(valor_aplicado)} acima do saldo devedor "
                f"({brl(cobranca.saldo)}) de '{cobranca.descricao}'."
            )
        AplicacaoRecebimento.objects.create(
            recebimento=recebimento, cobranca=cobranca, valor=valor_aplicado
        )
        cobranca.atualizar_status()
    sobra = valor - total
    if sobra > ZERO:
        if sobra_destino == "caucao":
            caucao = _caucao_ativa(cliente)
            if not caucao:
                raise ValidationError("Cliente sem caução ativa — sobra deve ir para crédito.")
            MovimentacaoCaucao.objects.create(
                caucao=caucao,
                tipo=MovimentacaoCaucao.Tipo.REFORCO,
                valor=sobra,
                data=data,
                forma=forma if forma != Recebimento.Forma.CREDITO else "",
                # a caução não tem vínculo com o recebimento: o nº no texto é o
                # que permite ao "Desfazer" achar este reforço depois
                observacoes=_marca_reforco(recebimento),
            )
        else:
            MovimentoCredito.objects.create(
                cliente=cliente,
                tipo=MovimentoCredito.Tipo.ENTRADA,
                valor=sobra,
                data=data,
                recebimento=recebimento,
                observacoes="Sobra de recebimento",
            )
    atualizar_inadimplencia(cliente, hoje=data)
    return recebimento


def _caucao_ativa(cliente):
    """Caução da alocação ativa do cliente — contrato encerrado não recebe reforço."""
    return (
        Caucao.objects.filter(alocacao__cliente=cliente, alocacao__status=Alocacao.Status.ATIVA)
        .order_by("-alocacao__data_inicio")
        .first()
    )


def _marca_reforco(recebimento):
    return f"Sobra do recebimento nº {recebimento.pk}"


def _reforco_do_recebimento(recebimento):
    return (
        MovimentacaoCaucao.objects.filter(
            tipo=MovimentacaoCaucao.Tipo.REFORCO, observacoes=_marca_reforco(recebimento)
        )
        .select_related("caucao")
        .first()
    )


def anotar_lancamento(queryset):
    """Anota `lancado_em` (quando o pagamento foi digitado) em lote.

    O Recebimento não guarda a hora do lançamento — `data` é o dia em que o
    cliente pagou; o momento do registro vem do histórico (simple_history).
    """
    historico = Recebimento.history.model.objects.filter(
        id=OuterRef("pk"), history_type="+"
    ).order_by("history_date")
    return queryset.annotate(lancado_em=Subquery(historico.values("history_date")[:1]))


def _recebimento_recente(cliente, data, valor):
    desde = timezone.now() - JANELA_RECEBIMENTO_REPETIDO
    return (
        anotar_lancamento(Recebimento.objects.filter(cliente=cliente, data=data, valor=valor))
        .filter(lancado_em__gte=desde)
        .order_by("-pk")
        .first()
    )


def pode_desfazer(recebimento, hoje=None):
    """Desfazer só vale no mesmo dia do lançamento (decisão do dono, proposta UX nº 1).

    Assim nunca se mexe num mês já passado à contabilidade (DAS) e o erro
    de digitação do dia se corrige sem a Retaguarda.
    """
    hoje = hoje or timezone.localdate()
    lancado_em = getattr(recebimento, "lancado_em", None)
    if not hasattr(recebimento, "lancado_em"):  # sem anotar_lancamento: busca no histórico
        primeiro = recebimento.history.filter(history_type="+").order_by("history_date").first()
        lancado_em = primeiro.history_date if primeiro else None
    return lancado_em is not None and timezone.localdate(lancado_em) == hoje


def _era_judicial(cobranca):
    """A cobrança quitada estava em cobrança judicial antes de virar PAGO?

    Lido do histórico ANTES de apagar as aplicações — o sinal de exclusão
    (signals.py) já recalcula o status e grava uma versão nova.
    """
    if cobranca.status != Cobranca.Status.PAGO:
        return False
    anterior = (
        cobranca.history.exclude(status=Cobranca.Status.PAGO).order_by("-history_date").first()
    )
    return bool(anterior and anterior.status == Cobranca.Status.JUDICIAL)


def _devolver_saldo(cobranca, judicial):
    """Reabre a cobrança que o recebimento desfeito tinha quitado (ou abatido).

    Cobrança judicial que virou PAGO volta a ser judicial.
    """
    cobranca.refresh_from_db(fields=["status"])
    if judicial and cobranca.status != Cobranca.Status.JUDICIAL:
        cobranca.status = Cobranca.Status.JUDICIAL
        cobranca.save(update_fields=["status"])
    cobranca.atualizar_status()


@transaction.atomic
def desfazer_recebimento(recebimento, hoje=None):
    """Apaga um lançamento feito hoje e volta tudo como estava antes dele.

    Devolve o saldo às cobranças, remove o crédito (ou o uso de crédito) e o
    reforço de caução gerados por ele e recalcula a inadimplência. O
    histórico (simple_history) guarda o registro apagado para auditoria.
    """
    if not pode_desfazer(recebimento, hoje):
        raise ValidationError(
            "Só dá para desfazer no mesmo dia em que o pagamento foi lançado. "
            "Para corrigir um lançamento de outro dia, use a Retaguarda."
        )
    cliente = Cliente.objects.select_for_update().get(pk=recebimento.cliente_id)
    if not Recebimento.objects.filter(pk=recebimento.pk).exists():
        # dois "Desfazer" ao mesmo tempo: o outro já apagou enquanto este esperava a trava
        raise ValidationError("Este lançamento já foi desfeito.")
    movimentos = recebimento.movimentos_credito.aggregate(
        entrada=Sum("valor", filter=Q(tipo=MovimentoCredito.Tipo.ENTRADA)),
        uso=Sum("valor", filter=Q(tipo=MovimentoCredito.Tipo.USO)),
    )
    entrada = movimentos["entrada"] or ZERO
    # o saldo depois de desfazer: sai a sobra que ele gerou, volta o crédito que ele usou
    if MovimentoCredito.saldo_do_cliente(cliente) - entrada + (movimentos["uso"] or ZERO) < ZERO:
        raise ValidationError(
            f"O crédito de {brl(entrada)} que este pagamento gerou já foi usado. "
            "Desfaça primeiro o pagamento que usou esse crédito."
        )
    reforco = _reforco_do_recebimento(recebimento)
    if reforco and reforco.valor > reforco.caucao.saldo:
        raise ValidationError(
            f"O reforço de caução de {brl(reforco.valor)} que este pagamento gerou já foi "
            "usado ou devolvido — não dá para desfazer por aqui."
        )
    cobrancas = [a.cobranca for a in recebimento.aplicacoes.select_related("cobranca")]
    judiciais = {c.pk for c in cobrancas if _era_judicial(c)}
    recebimento.movimentos_credito.all().delete()
    if reforco:
        reforco.delete()
    recebimento.delete()  # as aplicações vão junto (CASCADE)
    for cobranca in cobrancas:
        _devolver_saldo(cobranca, judicial=cobranca.pk in judiciais)
    atualizar_inadimplencia(cliente)
    return cobrancas


def resumo_recebimento(recebimento):
    """O que um lançamento fez, para o aviso pós-lançamento e o recibo.

    quitadas/parciais: (cobrança, valor aplicado); sobra: (valor, destino);
    devido: quanto o cliente ainda deve depois dele.
    """
    quitadas, parciais = [], []
    aplicacoes = recebimento.aplicacoes.select_related("cobranca").order_by(
        "cobranca__vencimento", "cobranca__pk"
    )
    for aplicacao in aplicacoes:
        destino = quitadas if aplicacao.cobranca.status == Cobranca.Status.PAGO else parciais
        destino.append((aplicacao.cobranca, aplicacao.valor))
    sobra = None
    entrada = recebimento.movimentos_credito.filter(tipo=MovimentoCredito.Tipo.ENTRADA).first()
    if entrada:
        sobra = (entrada.valor, "credito")
    else:
        reforco = _reforco_do_recebimento(recebimento)
        if reforco:
            sobra = (reforco.valor, "caucao")
    devidas = Cobranca.com_quitacao_anotada(
        recebimento.cliente.cobrancas.filter(status__in=Cobranca.STATUS_DEVIDOS)
    )
    return {
        "recebimento": recebimento,
        "quitadas": quitadas,
        "parciais": parciais,
        "sobra": sobra,
        "devido": sum((c.saldo for c in devidas), ZERO),
        "credito": MovimentoCredito.saldo_do_cliente(recebimento.cliente),
    }


def dias_pagamento_por_cliente():
    """{cliente_id: dia da semana (0 = segunda)} de quem tem dia de pagamento.

    Vale o dia da alocação ativa (é nele que o aluguel é cobrado); sem
    alocação ativa, o dia do cadastro do cliente. Duas consultas, para a
    tela da ND ajustar o vencimento ao trocar o cliente.
    """
    dias = {
        pk: dia
        for pk, dia in Cliente.objects.filter(dia_vencimento__isnull=False).values_list(
            "pk", "dia_vencimento"
        )
    }
    ativas = Alocacao.objects.filter(status=Alocacao.Status.ATIVA).order_by("data_inicio")
    dias.update(ativas.values_list("cliente_id", "dia_vencimento"))  # a mais recente vence
    return dias


def dia_pagamento(cliente):
    """Dia da semana em que o cliente paga, ou None (mesma regra de cima)."""
    alocacao = (
        cliente.alocacoes.filter(status=Alocacao.Status.ATIVA).order_by("-data_inicio").first()
    )
    if alocacao is not None:
        return alocacao.dia_vencimento
    return cliente.dia_vencimento


def proximo_dia_pagamento(cliente, a_partir_de, dia=...):
    """Vencimento padrão da ND (proposta #6, opção A): o próximo dia de pagamento.

    Sempre depois de `a_partir_de` — nunca no mesmo dia, senão a ND vira
    "atrasada" no dia seguinte e o motorista fica inadimplente sem ninguém
    decidir. Sem dia de pagamento conhecido: 7 dias depois.
    `dia` evita a consulta quando a tela já tem o dia em mãos.
    """
    if dia is ...:
        dia = dia_pagamento(cliente)
    if dia is None:
        return a_partir_de + timedelta(days=7)
    return a_partir_de + timedelta(days=(dia - a_partir_de.weekday()) % 7 or 7)


@transaction.atomic
def emitir_nota_debito(cliente, data_emissao, itens, vencimento=None, observacoes=""):
    """Emite ND com numeração automática e cria a cobrança única (docs.md §4.3).

    Sem vencimento informado, vence no próximo dia de pagamento do cliente.
    """
    itens = [(descricao, Decimal(valor)) for descricao, valor in itens if Decimal(valor) > ZERO]
    if not itens:
        raise ValidationError("A nota de débito precisa de pelo menos um item com valor.")
    if vencimento is None:
        vencimento = proximo_dia_pagamento(cliente, data_emissao)
    elif vencimento < data_emissao:
        raise ValidationError(
            f"O vencimento ({vencimento:%d/%m/%Y}) não pode ser antes da data de emissão "
            f"({data_emissao:%d/%m/%Y})."
        )
    nd = NotaDebito(
        cliente=cliente,
        data_emissao=data_emissao,
        vencimento=vencimento,
        observacoes=observacoes,
        numero=None,
    )
    nd.save()
    ItemNotaDebito.objects.bulk_create(
        ItemNotaDebito(nota_debito=nd, descricao=descricao, valor=valor)
        for descricao, valor in itens
    )
    Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.NOTA_DEBITO,
        descricao=f"Nota de débito ND {nd.numero:03d}",
        valor=nd.total,  # do banco: itens já arredondados a centavos (ex.: "10,555")
        vencimento=vencimento,
        nota_debito=nd,
    )
    return nd


@transaction.atomic
def abrir_caucao(alocacao, valor_recebido=None, data=None, forma=""):
    """Cria o registro de caução da alocação; deposita o valor se informado."""
    caucao, _ = Caucao.objects.get_or_create(alocacao=alocacao)
    if valor_recebido:
        MovimentacaoCaucao.objects.create(
            caucao=caucao,
            tipo=MovimentacaoCaucao.Tipo.RECEBIMENTO,
            valor=Decimal(valor_recebido),
            data=data or date.today(),
            forma=forma,
        )
    return caucao


@transaction.atomic
def descontar_da_caucao(caucao, cobranca, valor, data, observacoes=""):
    """Desconto na caução quitando (total ou parcialmente) uma cobrança."""
    valor = Decimal(valor)
    if valor <= ZERO:
        raise ValidationError("O valor do desconto deve ser maior que zero.")
    if valor > caucao.saldo:
        raise ValidationError(f"Desconto acima do saldo da caução ({brl(caucao.saldo)}).")
    if valor > cobranca.saldo:
        raise ValidationError(f"Desconto acima do saldo devedor ({brl(cobranca.saldo)}).")
    movimentacao = MovimentacaoCaucao.objects.create(
        caucao=caucao,
        tipo=MovimentacaoCaucao.Tipo.DESCONTO,
        valor=valor,
        data=data,
        cobranca=cobranca,
        observacoes=observacoes,
    )
    cobranca.atualizar_status()
    atualizar_inadimplencia(cobranca.cliente, hoje=data)
    return movimentacao


def resumo_fiscal(ano, mes):
    """Base de cálculo do DAS do mês (decisão nº 11): só a receita de locação.

    Regime de caixa: aplicações pela data do recebimento e descontos de
    caução pela data da movimentação — abater da caução tem o mesmo efeito
    de quitação (docs.md §4.3).
    """
    locacao = ZERO
    diversos = {}

    def somar(cobranca, valor):
        nonlocal locacao
        if cobranca.classificacao_fiscal == "locacao":
            locacao += valor
        else:
            rotulo = cobranca.get_origem_display()
            diversos[rotulo] = diversos.get(rotulo, ZERO) + valor

    aplicacoes = AplicacaoRecebimento.objects.filter(
        recebimento__data__year=ano, recebimento__data__month=mes
    ).select_related("cobranca")
    for aplicacao in aplicacoes:
        somar(aplicacao.cobranca, aplicacao.valor)
    descontos = MovimentacaoCaucao.objects.filter(
        tipo=MovimentacaoCaucao.Tipo.DESCONTO,
        data__year=ano,
        data__month=mes,
        cobranca__isnull=False,
    ).select_related("cobranca")
    for movimentacao in descontos:
        somar(movimentacao.cobranca, movimentacao.valor)
    caucao_recebida = (
        MovimentacaoCaucao.objects.filter(
            data__year=ano,
            data__month=mes,
            tipo__in=[MovimentacaoCaucao.Tipo.RECEBIMENTO, MovimentacaoCaucao.Tipo.REFORCO],
        ).aggregate(t=Sum("valor"))
    )["t"] or ZERO
    return {
        "locacao": locacao,
        "diversos": diversos,
        "total_diversos": sum(diversos.values(), ZERO),
        "caucao_recebida": caucao_recebida,
    }
