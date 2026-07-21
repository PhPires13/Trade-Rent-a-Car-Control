"""Regras de negócio do financeiro (docs.md §4.3, §4.4)."""

from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.alocacoes.models import Alocacao
from apps.pessoas.models import Cliente

from .models import BaixaCobranca, Cobranca, Recebimento

CENTAVO = Decimal("0.01")

# Encargos por atraso (docs.md decisão nº 13): 5% até 4 dias, 10% acima.
ENCARGO_ATE_DIAS = 4
ENCARGO_PERCENTUAL_BAIXO = Decimal("0.05")
ENCARGO_PERCENTUAL_ALTO = Decimal("0.10")

# Inadimplência a partir de 1 dia de atraso (docs.md decisão nº 14).
DIAS_PARA_INADIMPLENCIA = 1


def valor_semanal_vigente(alocacao, dia):
    """Valor semanal na data, considerando ajuste por troca temporária (docs.md §4.2)."""
    troca = (
        alocacao.trocas.filter(data_retirada__lte=dia, valor_semanal_ajustado__isnull=False)
        .filter(data_devolucao__isnull=True)
        .order_by("-data_retirada")
        .first()
    )
    if troca:
        return troca.valor_semanal_ajustado
    return alocacao.valor_semanal


@transaction.atomic
def gerar_cobrancas_semanais(hoje=None):
    """Gera a cobrança de aluguel de cada alocação ativa cujo dia de vencimento é hoje.

    Idempotente: não duplica a cobrança da mesma semana (docs.md §4.3).
    """
    hoje = hoje or date.today()
    inicio_semana = hoje - timedelta(days=6)
    criadas = []
    ativas = Alocacao.objects.filter(
        status=Alocacao.Status.ATIVA, dia_vencimento=hoje.weekday()
    ).select_related("cliente")
    for alocacao in ativas:
        if alocacao.data_inicio > hoje:
            continue
        ja_existe = Cobranca.objects.filter(
            alocacao=alocacao,
            origem=Cobranca.Origem.ALUGUEL,
            vencimento__gt=inicio_semana,
            vencimento__lte=hoje,
        ).exists()
        if ja_existe:
            continue
        criadas.append(
            Cobranca.objects.create(
                cliente=alocacao.cliente,
                alocacao=alocacao,
                origem=Cobranca.Origem.ALUGUEL,
                descricao=f"Semana até {hoje:%d/%m/%Y}",
                valor=valor_semanal_vigente(alocacao, hoje),
                vencimento=hoje,
            )
        )
    return criadas


def atualizar_atrasos_e_inadimplencia(hoje=None):
    """Marca cobranças atrasadas e clientes inadimplentes (docs.md §4.3)."""
    hoje = hoje or date.today()
    limite = hoje - timedelta(days=DIAS_PARA_INADIMPLENCIA - 1)
    atrasadas = Cobranca.objects.filter(
        status__in=[Cobranca.Status.PENDENTE, Cobranca.Status.PARCIAL],
        vencimento__lt=limite,
    )
    clientes_atrasados = set()
    for cobranca in atrasadas:
        if cobranca.saldo > 0:
            if cobranca.status != Cobranca.Status.PARCIAL:
                cobranca.status = Cobranca.Status.ATRASADO
                cobranca.save(update_fields=["status"])
            clientes_atrasados.add(cobranca.cliente_id)

    Cliente.objects.filter(id__in=clientes_atrasados, status=Cliente.Status.ATIVO).update(
        status=Cliente.Status.INADIMPLENTE
    )
    # Cliente sem cobranças atrasadas volta a ativo
    inadimplentes = Cliente.objects.filter(status=Cliente.Status.INADIMPLENTE)
    for cliente in inadimplentes:
        tem_atraso = cliente.cobrancas.filter(
            status__in=[Cobranca.Status.ATRASADO, Cobranca.Status.PARCIAL],
            vencimento__lt=limite,
        ).exists()
        if not tem_atraso:
            cliente.status = Cliente.Status.ATIVO
            cliente.save(update_fields=["status"])
    return clientes_atrasados


def sugerir_encargo(cobranca, hoje=None):
    """Percentual e valor sugeridos de encargo por atraso (docs.md decisão nº 13).

    O dono pode ajustar ou zerar o valor antes de confirmar.
    """
    hoje = hoje or date.today()
    dias_atraso = (hoje - cobranca.vencimento).days
    if dias_atraso <= 0 or cobranca.saldo <= 0:
        return {
            "dias_atraso": max(dias_atraso, 0),
            "percentual": Decimal("0"),
            "valor": Decimal("0"),
        }
    percentual = (
        ENCARGO_PERCENTUAL_BAIXO if dias_atraso <= ENCARGO_ATE_DIAS else ENCARGO_PERCENTUAL_ALTO
    )
    valor = (cobranca.saldo * percentual).quantize(CENTAVO)
    return {"dias_atraso": dias_atraso, "percentual": percentual, "valor": valor}


@transaction.atomic
def aplicar_encargo(cobranca, valor, hoje=None):
    """Cria a cobrança de encargo vinculada à cobrança atrasada."""
    hoje = hoje or date.today()
    valor = Decimal(valor).quantize(CENTAVO)
    if valor <= 0:
        return None
    return Cobranca.objects.create(
        cliente=cobranca.cliente,
        alocacao=cobranca.alocacao,
        origem=Cobranca.Origem.ENCARGO_ATRASO,
        descricao=f"Encargo sobre {cobranca.get_origem_display().lower()} "
        f"venc. {cobranca.vencimento:%d/%m/%Y}",
        valor=valor,
        vencimento=hoje,
        cobranca_origem=cobranca,
    )


def distribuir_automatico(cobrancas, valor_recebido):
    """Distribui o valor da cobrança mais antiga para a mais nova (docs.md §4.3)."""
    restante = Decimal(valor_recebido).quantize(CENTAVO)
    plano = {}
    for cobranca in sorted(cobrancas, key=lambda c: (c.vencimento, c.id)):
        if restante <= 0:
            break
        parcela = min(cobranca.saldo, restante)
        if parcela > 0:
            plano[cobranca.id] = parcela
            restante -= parcela
    return plano


@transaction.atomic
def registrar_recebimento(cliente, valor, data, forma, alocacoes_por_cobranca, observacoes=""):
    """Registra um recebimento e baixa as cobranças (docs.md §4.3).

    Travas: nenhuma parcela acima do saldo da cobrança; total alocado <= valor recebido.
    Retorna (recebimento, saldo_nao_alocado).
    """
    valor = Decimal(valor).quantize(CENTAVO)
    if valor <= 0:
        raise ValidationError("O valor recebido deve ser positivo.")

    itens = []
    total = Decimal("0")
    for cobranca_id, parcela in alocacoes_por_cobranca.items():
        parcela = Decimal(parcela).quantize(CENTAVO)
        if parcela <= 0:
            continue
        cobranca = Cobranca.objects.select_for_update().get(pk=cobranca_id)
        if cobranca.cliente_id != cliente.id:
            raise ValidationError("Cobrança de outro cliente na distribuição.")
        if parcela > cobranca.saldo:
            raise ValidationError(
                f"R$ {parcela} excede o saldo de R$ {cobranca.saldo} da cobrança "
                f"'{cobranca.get_origem_display()}'."
            )
        itens.append((cobranca, parcela))
        total += parcela

    if total > valor:
        raise ValidationError(
            f"Total alocado (R$ {total}) maior que o valor recebido (R$ {valor})."
        )

    recebimento = Recebimento.objects.create(
        cliente=cliente, valor=valor, data=data, forma=forma, observacoes=observacoes
    )
    for cobranca, parcela in itens:
        BaixaCobranca.objects.create(recebimento=recebimento, cobranca=cobranca, valor=parcela)
        cobranca.atualizar_status()
    return recebimento, recebimento.saldo_nao_alocado


def cobrancas_em_aberto(cliente):
    return (
        cliente.cobrancas.exclude(status=Cobranca.Status.PAGO)
        .select_related("alocacao__veiculo")
        .order_by("vencimento", "id")
    )
