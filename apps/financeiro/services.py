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
from django.db.models import Q, Sum

from apps.alocacoes.models import Alocacao
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


def _proximo_vencimento(alocacao):
    """Primeira data >= início da locação que cai no dia de vencimento."""
    data = alocacao.data_inicio
    while data.weekday() != alocacao.dia_vencimento:
        data += timedelta(days=1)
    return data


def _valor_da_semana(alocacao, vencimento):
    """Valor semanal vigente no vencimento — troca temporária pode ajustar (decisão nº 1)."""
    troca = (
        alocacao.trocas.filter(data_retirada__lte=vencimento, valor_semanal_ajustado__isnull=False)
        .filter(Q(data_devolucao__isnull=True) | Q(data_devolucao__gte=vencimento))
        .first()
    )
    return troca.valor_semanal_ajustado if troca else alocacao.valor_semanal


def gerar_cobrancas_semanais(hoje=None):
    """Gera as cobranças de aluguel vencidas até hoje (idempotente; recupera dias perdidos)."""
    hoje = hoje or date.today()
    criadas = []
    alocacoes = Alocacao.objects.filter(
        Q(status=Alocacao.Status.ATIVA) | Q(data_termino__gte=hoje - timedelta(days=60))
    ).select_related("cliente", "veiculo")
    for alocacao in alocacoes:
        vencimento = _proximo_vencimento(alocacao)
        limite = min(hoje, alocacao.data_termino or hoje)
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


def marcar_atrasos(hoje=None):
    """Atualiza cobranças atrasadas e a inadimplência dos clientes (decisão nº 14)."""
    hoje = hoje or date.today()
    limite = hoje - timedelta(days=DIAS_PARA_INADIMPLENCIA)
    abertas = Cobranca.objects.filter(
        status__in=[Cobranca.Status.PENDENTE, Cobranca.Status.PARCIAL, Cobranca.Status.ATRASADO]
    )
    for cobranca in abertas:
        cobranca.atualizar_status(hoje)
    inadimplentes = set(
        Cobranca.objects.filter(
            status=Cobranca.Status.ATRASADO, vencimento__lte=limite
        ).values_list("cliente_id", flat=True)
    )
    Cliente.objects.filter(pk__in=inadimplentes).exclude(status=Cliente.Status.INATIVO).update(
        status=Cliente.Status.INADIMPLENTE
    )
    Cliente.objects.filter(status=Cliente.Status.INADIMPLENTE).exclude(pk__in=inadimplentes).update(
        status=Cliente.Status.ATIVO
    )


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
    cliente, data, valor, forma, aplicacoes, sobra_destino="credito", observacoes=""
):
    """Lança um pagamento e distribui nas cobranças, com travas (docs.md §4.3).

    aplicacoes: lista de (cobranca, valor). Travas: nenhuma aplicação acima do
    saldo da cobrança; total aplicado nunca acima do recebido. Sobra vira
    crédito do cliente ou reforço de caução.
    """
    valor = Decimal(valor)
    if valor <= ZERO:
        raise ValidationError("Valor recebido deve ser maior que zero.")
    total = sum((Decimal(v) for _, v in aplicacoes), ZERO)
    if total > valor:
        raise ValidationError(
            f"Total alocado (R$ {total}) maior que o valor recebido (R$ {valor})."
        )
    if forma == Recebimento.Forma.CREDITO:
        saldo_credito = MovimentoCredito.saldo_do_cliente(cliente)
        if valor > saldo_credito:
            raise ValidationError(f"Crédito insuficiente (saldo R$ {saldo_credito}).")
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
                f"Aplicação de R$ {valor_aplicado} acima do saldo devedor "
                f"(R$ {cobranca.saldo}) de '{cobranca.descricao}'."
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
                observacoes="Sobra de recebimento",
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
    return recebimento


def _caucao_ativa(cliente):
    return (
        Caucao.objects.filter(alocacao__cliente=cliente).order_by("-alocacao__data_inicio").first()
    )


def distribuicao_automatica(cliente, valor):
    """Sugestão: distribui o valor da cobrança mais antiga para a mais nova."""
    valor = Decimal(valor)
    sugestao = []
    abertas = cliente.cobrancas.filter(
        status__in=[Cobranca.Status.PENDENTE, Cobranca.Status.PARCIAL, Cobranca.Status.ATRASADO]
    ).order_by("vencimento")
    for cobranca in abertas:
        if valor <= ZERO:
            break
        aplicar = min(valor, cobranca.saldo)
        if aplicar > ZERO:
            sugestao.append((cobranca, aplicar))
            valor -= aplicar
    return sugestao


@transaction.atomic
def emitir_nota_debito(cliente, data_emissao, itens, vencimento=None, observacoes=""):
    """Emite ND com numeração automática e cria a cobrança única (docs.md §4.3)."""
    itens = [(descricao, Decimal(valor)) for descricao, valor in itens if Decimal(valor) > ZERO]
    if not itens:
        raise ValidationError("A nota de débito precisa de pelo menos um item com valor.")
    nd = NotaDebito(
        cliente=cliente,
        data_emissao=data_emissao,
        vencimento=vencimento,
        observacoes=observacoes,
        numero=None,
    )
    nd.save()
    for descricao, valor in itens:
        ItemNotaDebito.objects.create(nota_debito=nd, descricao=descricao, valor=valor)
    Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.NOTA_DEBITO,
        descricao=f"Nota de débito ND {nd.numero:03d}",
        valor=nd.total,
        vencimento=vencimento or data_emissao,
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
    if valor > caucao.saldo:
        raise ValidationError(f"Desconto acima do saldo da caução (R$ {caucao.saldo}).")
    if valor > cobranca.saldo:
        raise ValidationError(f"Desconto acima do saldo devedor (R$ {cobranca.saldo}).")
    movimentacao = MovimentacaoCaucao.objects.create(
        caucao=caucao,
        tipo=MovimentacaoCaucao.Tipo.DESCONTO,
        valor=valor,
        data=data,
        cobranca=cobranca,
        observacoes=observacoes,
    )
    cobranca.atualizar_status()
    return movimentacao


def resumo_fiscal(ano, mes):
    """Base de cálculo do DAS do mês (decisão nº 11): só a receita de locação.

    Regime de caixa: soma as aplicações pela data do recebimento.
    """
    aplicacoes = AplicacaoRecebimento.objects.filter(
        recebimento__data__year=ano, recebimento__data__month=mes
    ).select_related("cobranca", "recebimento")
    locacao = ZERO
    diversos = {}
    for aplicacao in aplicacoes:
        if aplicacao.cobranca.classificacao_fiscal == "locacao":
            locacao += aplicacao.valor
        else:
            rotulo = aplicacao.cobranca.get_origem_display()
            diversos[rotulo] = diversos.get(rotulo, ZERO) + aplicacao.valor
    caucao_recebida = (
        MovimentacaoCaucao.objects.filter(
            data__year=ano, data__month=mes, tipo__in=["recebimento", "reforco"]
        ).aggregate(t=Sum("valor"))
    )["t"] or ZERO
    return {
        "locacao": locacao,
        "diversos": diversos,
        "total_diversos": sum(diversos.values(), ZERO),
        "caucao_recebida": caucao_recebida,
    }
