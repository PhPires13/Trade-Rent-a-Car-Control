"""Regras de negócio do financeiro (docs.md §4.3–4.4).

Decisões dos donos aplicadas aqui:
- nº 7: cobrança semanal no dia de vencimento de cada cliente
- nº 13: encargos 5% (até ~4 dias) / 10% (acima), ajustáveis caso a caso
- nº 14: inadimplente a partir de 1 dia de atraso
- nº 11: DAS calculado só sobre a receita de locação
"""

from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, OuterRef, Q, Subquery, Sum
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


#: data de pagamento/caução antes disto é ano digitado errado ("0026", "1926")
ANO_MINIMO_PAGAMENTO = 2000
#: tamanho máximo das observações digitadas nas telas do financeiro
MAX_OBSERVACOES = 500


def limpar_texto(texto, maximo=MAX_OBSERVACOES):
    """Observação digitada à mão: sem caractere NUL (o Postgres recusa) e com tamanho sensato."""
    return str(texto or "").replace("\x00", "").strip()[:maximo]


def inicio_do_mes_passado(hoje):
    """1º dia do mês anterior a `hoje` — antes disso, o DAS já foi para a contabilidade."""
    primeiro = hoje.replace(day=1)
    return (primeiro - timedelta(days=1)).replace(day=1)


def validar_data_pagamento(data, hoje=None, mes_passado_confirmado=True):
    """Data em que o dinheiro entrou: nunca no futuro nem com ano impossível.

    O pagamento entra no DAS do mês da data; uma data errada mexe no imposto
    de outro mês. Antes do mês passado, só com a confirmação explícita
    ("Confirmo: pagamento de mês passado — o DAS daquele mês muda").
    """
    hoje = hoje or timezone.localdate()
    if data.year < ANO_MINIMO_PAGAMENTO:
        raise ValidationError(f"Confira o ano da data do pagamento ({data:%d/%m/%Y}).")
    if data > hoje:
        raise ValidationError("A data do pagamento não pode ser no futuro.")
    if data < inicio_do_mes_passado(hoje) and not mes_passado_confirmado:
        raise ValidationError(
            f"A data {data:%d/%m/%Y} é de um mês que já passou: o DAS daquele mês muda. "
            'Se a data estiver certa, marque "Confirmo: pagamento de mês passado".'
        )


class RecebimentoRepetido(ValidationError):
    """O mesmo pagamento acabou de ser lançado (toque duplo, 4G lento)."""

    def __init__(self, recebimento):
        super().__init__(
            "Este pagamento já foi lançado agora há pouco — não foi lançado de novo. "
            "Se for mesmo um segundo pagamento igual, espere 2 minutos e lance outra vez."
        )
        self.recebimento = recebimento


def _proximo_dia(data, dia_semana):
    """Primeiro dia >= data que cai no dia da semana pedido (0 = segunda)."""
    return data + timedelta(days=(dia_semana - data.weekday()) % 7)


def periodo_da_cobranca(cobranca):
    """Dias de aluguel pagos pela cobrança (antigas, sem período: a semana do vencimento)."""
    inicio = cobranca.periodo_inicio or cobranca.vencimento
    fim = cobranca.periodo_fim or cobranca.vencimento + timedelta(days=6)
    return inicio, fim


def _data_da_mudanca_do_dia(alocacao):
    """Dia em que o dia de vencimento atual passou a valer (histórico), ou None.

    None quando o dia nunca mudou ou o histórico não diz quando.
    """
    historico = alocacao.history.all()
    anterior = (
        historico.exclude(dia_vencimento=alocacao.dia_vencimento).order_by("-history_date").first()
    )
    if anterior is None:
        return None
    mudanca = (
        historico.filter(
            history_date__gt=anterior.history_date, dia_vencimento=alocacao.dia_vencimento
        )
        .order_by("history_date")
        .first()
    )
    return timezone.localdate(mudanca.history_date) if mudanca else None


def _proximo_vencimento(alocacao, hoje=None, ultima=...):
    """(vencimento, primeiro dia ainda não cobrado) da próxima cobrança de aluguel.

    Ancorado na última cobrança existente (inclusive cancelada), para que uma
    mudança de dia_vencimento em contrato ativo valha só para frente — sem
    regenerar semanas já cobradas nem semanas canceladas. Sem cobrança ainda,
    começa no "cobrar a partir de" (carga de contrato antigo, decisão D01) ou
    no início da locação.

    Dia de vencimento mudado (D04): o 1º vencimento no dia novo é procurado a
    partir do dia da mudança — a cobrança de transição nunca nasce vencida
    (antes, mudar quarta→sexta numa segunda criava uma cobrança com vencimento
    na sexta anterior, já atrasada, e o cliente em dia virava inadimplente).
    """
    hoje = hoje or timezone.localdate()
    if ultima is ...:
        ultima = (
            Cobranca.objects.filter(alocacao=alocacao, origem=Cobranca.Origem.ALUGUEL)
            .order_by("-vencimento")
            .first()
        )
    if ultima is None:
        inicio = alocacao.cobrar_a_partir_de or alocacao.data_inicio
        return inicio, inicio
    busca = ultima.vencimento + timedelta(days=1)
    vencimento = _proximo_dia(busca, alocacao.dia_vencimento)
    if ultima.vencimento.weekday() != alocacao.dia_vencimento and vencimento < hoje:
        mudanca = _data_da_mudanca_do_dia(alocacao)
        # mudança "depois de hoje" só acontece reprocessando o passado: vale a conta normal
        if mudanca is not None and busca < mudanca <= hoje:
            vencimento = _proximo_dia(mudanca, alocacao.dia_vencimento)
    return vencimento, periodo_da_cobranca(ultima)[1] + timedelta(days=1)


def proximo_vencimento(alocacao, hoje=None):
    """(vencimento, 1º dia ainda não cobrado) do próximo aluguel da alocação.

    Pública para o Painel prever os aluguéis da semana com a mesma conta da
    rotina que os gera (inclusive a troca do dia de vencimento, D04).
    """
    return _proximo_vencimento(alocacao, hoje)


def _valor_do_periodo(alocacao, inicio, fim, valor_base=None):
    """Valor dos dias [inicio, fim] pelo preço semanal ÷ 7 de cada dia (docs.md §4.2).

    Troca temporária com valor ajustado vale pelos dias em que está em curso;
    o restante sai pelo valor da alocação (decisão nº 1) — ou por `valor_base`
    (preço semanal que a própria cobrança já usava, no reajuste da troca).
    Semana cheia sem troca = valor semanal exato.
    """
    base = alocacao.valor_semanal if valor_base is None else valor_base
    dias = (fim - inicio).days + 1
    trocas = list(
        alocacao.trocas.filter(valor_semanal_ajustado__isnull=False, data_retirada__lte=fim).filter(
            Q(data_devolucao__isnull=True) | Q(data_devolucao__gte=inicio)
        )
    )
    if not trocas and dias == 7:
        return base
    total = ZERO
    for i in range(dias):
        dia = inicio + timedelta(days=i)
        vigente = next(
            (
                troca.valor_semanal_ajustado
                for troca in trocas
                if troca.data_retirada <= dia
                and (troca.data_devolucao is None or troca.data_devolucao >= dia)
            ),
            base,
        )
        total += vigente
    return (total / 7).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _valor_da_semana(alocacao, vencimento):
    """Valor da semana [vencimento, vencimento+6], rateado por dia (docs.md §4.2)."""
    return _valor_do_periodo(alocacao, vencimento, vencimento + timedelta(days=6))


def _descricao_do_aluguel(alocacao, vencimento, inicio, fim):
    placa = alocacao.veiculo.placa
    if inicio == vencimento and fim == vencimento + timedelta(days=6):
        return f"Aluguel semanal {placa} — venc. {vencimento:%d/%m/%Y}"
    dias = (fim - inicio).days + 1
    rotulo = "1 dia" if dias == 1 else f"{dias} dias"
    return f"Aluguel proporcional {placa} — {rotulo} ({inicio:%d/%m} a {fim:%d/%m/%Y})"


def gerar_cobrancas_semanais(hoje=None):
    """Gera as cobranças de aluguel vencidas até hoje (idempotente; recupera dias perdidos).

    Cada cobrança paga os dias ainda não cobertos até a véspera do próximo
    vencimento — sem dia de graça nem dia em dobro:
    - 1ª cobrança no dia da entrega; se o dia de pagar é outro, ela sai
      proporcional, só pelos dias até o 1º vencimento (decisão D12);
    - mudou o dia de vencimento: a 1ª cobrança no dia novo sai proporcional,
      só pelos dias que a semana anterior não cobriu (decisão D04).
    """
    hoje = hoje or timezone.localdate()
    criadas = []
    alocacoes = Alocacao.objects.filter(
        Q(status=Alocacao.Status.ATIVA) | Q(data_termino__gte=hoje - timedelta(days=60))
    ).select_related("cliente", "veiculo")
    for alocacao in alocacoes:
        vencimento, inicio = _proximo_vencimento(alocacao, hoje)
        # Cobrança pré-paga: a semana só é devida se começa ANTES da devolução.
        if alocacao.data_termino:
            limite = min(hoje, alocacao.data_termino - timedelta(days=1))
        else:
            limite = hoje
        while vencimento <= limite:
            if alocacao.data_termino and inicio >= alocacao.data_termino:
                break  # transição do dia (D04) que só pagaria dias depois da devolução
            proximo = _proximo_dia(vencimento + timedelta(days=1), alocacao.dia_vencimento)
            fim = proximo - timedelta(days=1)
            if fim >= inicio:
                cobranca, criada = Cobranca.objects.get_or_create(
                    alocacao=alocacao,
                    vencimento=vencimento,
                    origem=Cobranca.Origem.ALUGUEL,
                    defaults={
                        "cliente": alocacao.cliente,
                        "descricao": _descricao_do_aluguel(alocacao, vencimento, inicio, fim),
                        "valor": _valor_do_periodo(alocacao, inicio, fim),
                        "periodo_inicio": inicio,
                        "periodo_fim": fim,
                    },
                )
                if criada:
                    criadas.append(cobranca)
                inicio = periodo_da_cobranca(cobranca)[1] + timedelta(days=1)
            vencimento = proximo
    return criadas


@transaction.atomic
def reajustar_aluguel_pela_troca(troca, hoje=None):
    """Refaz o valor das semanas de aluguel em aberto que a troca alcança (decisão D02).

    Carro substituto com outro preço pego ou devolvido no meio de uma semana
    já lançada: a semana passa a dividir os dias entre cada carro. Só mexe em
    cobrança ainda em aberto e quando o novo valor não fica abaixo do que já
    foi pago — semana paga, cancelada ou judicial fica como está.
    Devolve as cobranças alteradas.
    """
    if troca.valor_semanal_ajustado is None:
        return []
    alocacao = troca.alocacao
    abertas = Cobranca.com_quitacao_anotada(
        Cobranca.objects.filter(
            alocacao=alocacao,
            origem=Cobranca.Origem.ALUGUEL,
            status__in=Cobranca.STATUS_EM_ABERTO,
            vencimento__gte=troca.data_retirada - timedelta(days=6),
        )
    )
    alteradas = []
    for cobranca in abertas:
        inicio, fim = periodo_da_cobranca(cobranca)
        if fim < troca.data_retirada:
            continue
        # dias fora da troca ficam com o preço que a semana já tinha (não o do
        # contrato editado depois): só os dias da troca mudam de preço
        novo = _valor_do_periodo(alocacao, inicio, fim, _preco_semanal_da_cobranca(cobranca))
        if novo == cobranca.valor or novo < cobranca.total_quitado:
            continue
        cobranca.valor = novo
        cobranca._change_reason = MOTIVO_TROCA
        cobranca.save(update_fields=["valor"])
        cobranca.atualizar_status(hoje)
        alteradas.append(cobranca)
    if alteradas:
        atualizar_inadimplencia(alocacao.cliente)  # semana pode ter ficado quitada
    return alteradas


#: motivo gravado no histórico quando o valor da semana muda por troca temporária
MOTIVO_TROCA = "troca temporária"


def _preco_semanal_da_cobranca(cobranca):
    """Preço semanal do contrato quando o valor da cobrança foi calculado por último.

    Procura no histórico a última vez em que o valor mudou sem ser por troca
    (criação pela rotina ou correção do contrato) e lê o valor semanal que a
    alocação tinha naquele momento. Sem histórico: o valor atual do contrato.
    """
    alocacao = cobranca.alocacao
    momento, anterior = None, None
    for versao in cobranca.history.order_by("history_date", "history_id"):
        if versao.valor != anterior and versao.history_change_reason != MOTIVO_TROCA:
            momento = versao.history_date
        anterior = versao.valor
    if momento is None:
        return alocacao.valor_semanal
    contrato = alocacao.history.filter(history_date__lte=momento).order_by("-history_date").first()
    return contrato.valor_semanal if contrato else alocacao.valor_semanal


@transaction.atomic
def refazer_alugueis_em_aberto(alocacao, hoje=None):
    """Recalcula os aluguéis ainda devidos depois de corrigir o valor semanal do contrato.

    Ex.: alocação lançada com 6.500,00 em vez de 650,00 — ao corrigir, a semana
    em aberto (que não pode ser cancelada, por ser aluguel) passa ao valor certo.
    Só mexe em aluguel pendente/parcial/atrasado (judicial e pago ficam como
    estão) e nunca abaixo do que já foi pago — mesma regra do reajuste da
    troca. Devolve as cobranças alteradas.
    """
    devidas = Cobranca.com_quitacao_anotada(
        Cobranca.objects.filter(
            alocacao=alocacao,
            origem=Cobranca.Origem.ALUGUEL,
            status__in=Cobranca.STATUS_EM_ABERTO,
        )
    )
    alteradas = []
    for cobranca in devidas:
        novo = _valor_do_periodo(alocacao, *periodo_da_cobranca(cobranca))
        if novo == cobranca.valor or novo < cobranca.total_quitado:
            continue
        cobranca.valor = novo
        cobranca.save(update_fields=["valor"])
        cobranca.atualizar_status(hoje)
        alteradas.append(cobranca)
    if alteradas:
        atualizar_inadimplencia(alocacao.cliente, hoje)
    return alteradas


def _filtro_devedor(limite):
    """Cobrança que torna o cliente devedor: atrasada há >= 1 dia ou judicial (nº 14 e 17)."""
    return Q(status=Cobranca.Status.ATRASADO, vencimento__lte=limite) | Q(
        status=Cobranca.Status.JUDICIAL
    )


def marcar_atrasos(hoje=None):
    """Atualiza cobranças atrasadas e a inadimplência dos clientes (decisões nº 14 e 17)."""
    hoje = hoje or timezone.localdate()
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
    hoje = hoje or timezone.localdate()
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


def sugerir_encargo(cobranca, hoje=None, ja_aplicado=None, ativos=None):
    """5% até ~4 dias de atraso, 10% acima — sempre ajustável pelo dono (decisão nº 13).

    Só para aluguel (ponto em aberto nº 3: ND, repasse, excedente e o próprio
    encargo não têm encargo). Já aplicados os 5% e o atraso passou de 4 dias,
    sugere o complemento até 10% (um só). `ja_aplicado`/`ativos` (soma e
    quantidade de encargos não cancelados) evitam as consultas na listagem.
    """
    hoje = hoje or timezone.localdate()
    if cobranca.origem != Cobranca.Origem.ALUGUEL:
        return ZERO
    dias = (hoje - cobranca.vencimento).days
    if dias <= 0 or cobranca.saldo <= ZERO:
        return ZERO
    if ja_aplicado is None or ativos is None:
        somas = cobranca.encargos.exclude(status=Cobranca.Status.CANCELADA).aggregate(
            t=Sum("valor"), n=Count("pk")
        )
        ja_aplicado, ativos = somas["t"] or ZERO, somas["n"]
    if ativos >= 2 or (ativos and dias <= ENCARGO_DIAS_CORTE):
        return ZERO
    taxa = ENCARGO_CURTO if dias <= ENCARGO_DIAS_CORTE else ENCARGO_LONGO
    total = (cobranca.saldo * taxa).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return max(total - ja_aplicado, ZERO)


def encargo_maximo(cobranca):
    """Teto do encargo: 10% do saldo do aluguel, menos os encargos já aplicados (nº 13)."""
    ja_aplicado = (
        cobranca.encargos.exclude(status=Cobranca.Status.CANCELADA).aggregate(t=Sum("valor"))["t"]
        or ZERO
    )
    teto = (max(cobranca.saldo, ZERO) * ENCARGO_LONGO).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return max(teto - ja_aplicado, ZERO)


def valor_do_encargo(texto, cobranca, hoje=None):
    """Lê o valor digitado no encargo: "5%"/"10%" vira reais sobre o saldo do aluguel.

    Devolve None quando o texto não é porcentagem (a view lê como dinheiro).
    """
    texto = (texto or "").strip().replace(" ", "")
    if not texto.endswith("%"):
        return None
    try:
        taxa = Decimal(texto[:-1].replace(",", ".")) / 100
    except ArithmeticError:
        raise ValidationError("Porcentagem inválida — use 5% ou 10%.") from None
    if taxa <= ZERO:
        raise ValidationError("O valor do encargo deve ser maior que zero.")
    return (max(cobranca.saldo, ZERO) * taxa).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@transaction.atomic
def aplicar_encargo(cobranca, valor, hoje=None):
    """Cria a cobrança de encargo vinculada à atrasada (fora da base do DAS).

    Só sobre aluguel (nunca encargo sobre encargo). Um encargo por aluguel,
    mais um complemento depois de 4 dias de atraso (5% → 10%). Vence no
    próximo dia de pagamento do cliente — vencer hoje deixaria o motorista
    inadimplente amanhã sem ninguém decidir.
    """
    hoje = hoje or timezone.localdate()
    if valor <= ZERO:
        raise ValidationError("O valor do encargo deve ser maior que zero.")
    if cobranca.origem != Cobranca.Origem.ALUGUEL:
        raise ValidationError("Encargo por atraso só pode ser cobrado sobre aluguel.")
    # trava a cobrança: clique duplo no "Aplicar" não vira dois encargos
    Cobranca.objects.select_for_update().filter(pk=cobranca.pk).first()
    # tela aberta há tempo: a semana pode ter sido paga (ou cancelada) noutra aba
    cobranca.refresh_from_db()
    cobranca.__dict__.pop("_total_quitado_anotado", None)
    cobranca.atualizar_status(hoje)
    if cobranca.status != Cobranca.Status.ATRASADO or cobranca.saldo <= ZERO:
        raise ValidationError(
            f"Esta cobrança não está atrasada (situação: {cobranca.get_status_display()}) "
            "— encargo não se aplica. A tela foi atualizada."
        )
    ativos = cobranca.encargos.exclude(status=Cobranca.Status.CANCELADA).count()
    dias = (hoje - cobranca.vencimento).days
    if ativos >= 2 or (ativos == 1 and dias <= ENCARGO_DIAS_CORTE):
        raise ValidationError("Esta cobrança já tem encargo aplicado.")
    if ativos == 1 and sugerir_encargo(cobranca, hoje) <= ZERO:
        # o encargo já lançado cobre os 10%: complemento seria cobrar em dobro
        raise ValidationError("O encargo já aplicado nesta cobrança já chega aos 10%.")
    maximo = encargo_maximo(cobranca)
    if valor > maximo:
        raise ValidationError(
            f"Encargo de {brl(valor)} acima do permitido: o máximo pela regra é "
            f"{brl(maximo)} (10% do que falta pagar"
            + (", menos o encargo já aplicado)." if ativos else ").")
        )
    rotulo = "Complemento do encargo por atraso" if ativos else "Encargo por atraso"
    return Cobranca.objects.create(
        cliente=cobranca.cliente,
        alocacao=cobranca.alocacao,
        origem=Cobranca.Origem.ENCARGO,
        descricao=f"{rotulo} — {cobranca.descricao}",
        valor=valor,
        vencimento=proximo_dia_pagamento(cobranca.cliente, hoje),
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
    usar_credito=ZERO,
    mes_passado_confirmado=True,
    hoje=None,
):
    """Lança um pagamento e distribui nas cobranças, com travas (docs.md §4.3).

    aplicacoes: lista de (cobranca, valor). Travas: nenhuma aplicação acima do
    saldo da cobrança; total aplicado nunca acima do recebido. Sobra vira
    crédito do cliente ou reforço de caução.

    bloquear_repetido (tela de receber): o mesmo cliente + valor + data lançado
    há menos de 2 minutos levanta RecebimentoRepetido — toque duplo no
    Confirmar com internet lenta não vira pagamento em dobro.

    usar_credito: parte do crédito guardado que entra junto com o dinheiro
    novo (ex.: 50 de crédito + Pix de 600 = semana de 650). Vira um lançamento
    à parte, forma "crédito", nas cobranças mais antigas — o DAS separa o que
    foi pago com crédito do dinheiro novo. O lançamento de crédito fica em
    `recebimento.recebimento_credito` (None sem crédito).

    A data passa por validar_data_pagamento (nunca no futuro; mês retrasado
    só com `mes_passado_confirmado`).
    """
    valor = Decimal(valor)
    usar_credito = Decimal(usar_credito or ZERO)
    if valor <= ZERO:
        raise ValidationError("Valor recebido deve ser maior que zero.")
    if usar_credito < ZERO:
        raise ValidationError("O crédito a usar não pode ser negativo.")
    if forma not in Recebimento.Forma.values:
        raise ValidationError('Escolha em "Como pagou" a forma do pagamento.')
    if usar_credito > ZERO and forma == Recebimento.Forma.CREDITO:
        raise ValidationError("Pagamento só com crédito não usa crédito a mais.")
    validar_data_pagamento(data, hoje, mes_passado_confirmado)
    observacoes = limpar_texto(observacoes)
    # trava a linha do cliente: dois lançamentos simultâneos esperam um pelo outro
    # (sem isso, os dois liam o mesmo saldo e pagavam a mesma semana duas vezes)
    Cliente.objects.select_for_update().filter(pk=cliente.pk).first()
    if bloquear_repetido:
        repetido = _recebimento_recente(cliente, data, valor)
        if repetido:
            raise RecebimentoRepetido(repetido)
    aplicacoes = [(c, Decimal(v)) for c, v in aplicacoes if Decimal(v) > ZERO]
    total = sum((v for _, v in aplicacoes), ZERO)
    if total > valor + usar_credito:
        raise ValidationError(
            f"Total alocado ({brl(total)}) maior que o valor recebido ({brl(valor)})"
            + (f" mais o crédito usado ({brl(usar_credito)})." if usar_credito else ".")
        )
    saldo_credito = None
    if forma == Recebimento.Forma.CREDITO or usar_credito > ZERO:
        saldo_credito = MovimentoCredito.saldo_do_cliente(cliente)
        pedido = valor if forma == Recebimento.Forma.CREDITO else usar_credito
        if pedido > saldo_credito:
            raise ValidationError(f"Crédito insuficiente (saldo {brl(saldo_credito)}).")
    # saldo relido com a cobrança travada: o saldo que a tela carregou pode já
    # ter sido pago por outro lançamento (não confiar no valor anotado)
    frescas = {
        c.pk: c
        for c in Cobranca.objects.select_for_update().filter(pk__in=[c.pk for c, _ in aplicacoes])
    }
    conferidas = []
    for cobranca, valor_aplicado in aplicacoes:
        atual = frescas.get(cobranca.pk)
        if atual is None or atual.cliente_id != cliente.pk:
            raise ValidationError(f"Cobrança {cobranca} não é deste cliente.")
        if atual.status not in Cobranca.STATUS_DEVIDOS or atual.saldo <= ZERO:
            raise ValidationError(
                f"'{atual.descricao}' já não está em aberto (foi quitada ou cancelada). "
                "A tela foi atualizada — confira e lance de novo."
            )
        if valor_aplicado > atual.saldo:
            raise ValidationError(
                f"Aplicação de {brl(valor_aplicado)} acima do saldo devedor "
                f"({brl(atual.saldo)}) de '{atual.descricao}'."
            )
        conferidas.append((atual, valor_aplicado))

    # crédito usado junto: cobre as cobranças mais antigas primeiro
    credito_usado = min(usar_credito, total)
    pelo_credito, pelo_dinheiro = [], []
    resta = credito_usado
    for cobranca, valor_aplicado in sorted(
        conferidas, key=lambda par: (par[0].vencimento, par[0].pk)
    ):
        parte = min(resta, valor_aplicado)
        if parte > ZERO:
            pelo_credito.append((cobranca, parte))
            resta -= parte
        if valor_aplicado - parte > ZERO:
            pelo_dinheiro.append((cobranca, valor_aplicado - parte))

    recebimento_credito = None
    if credito_usado > ZERO:
        recebimento_credito = _lancar(
            cliente, data, credito_usado, Recebimento.Forma.CREDITO, pelo_credito, observacoes
        )
    else:
        pelo_dinheiro = conferidas
    recebimento = _lancar(cliente, data, valor, forma, pelo_dinheiro, observacoes)
    recebimento.recebimento_credito = recebimento_credito
    sobra = valor - (total - credito_usado)
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
    # inadimplência pela data de hoje: um pagamento lançado com data antiga não
    # pode esconder as semanas que atrasaram depois dela (a data vale p/ caixa/DAS)
    atualizar_inadimplencia(cliente)
    return recebimento


def _lancar(cliente, data, valor, forma, aplicacoes, observacoes):
    """Grava um Recebimento e as aplicações dele (saldos já conferidos)."""
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
        AplicacaoRecebimento.objects.create(
            recebimento=recebimento, cobranca=cobranca, valor=valor_aplicado
        )
        cobranca.atualizar_status()
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


def marca_encargo(recebimento):
    """Texto que liga o encargo cobrado junto no Receber ao lançamento (para o desfazer)."""
    return f"Cobrado junto com o recebimento nº {recebimento.pk}"


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
    marca = marca_encargo(recebimento)
    recebimento.delete()  # as aplicações vão junto (CASCADE)
    for cobranca in cobrancas:
        _devolver_saldo(cobranca, judicial=cobranca.pk in judiciais)
    # encargo criado junto com este lançamento (Receber) sai junto, se ficou sem pagamento
    for encargo in Cobranca.objects.filter(
        cliente=cliente, origem=Cobranca.Origem.ENCARGO, observacoes=marca
    ).exclude(status=Cobranca.Status.CANCELADA):
        if encargo.total_quitado <= ZERO:
            encargo.status = Cobranca.Status.CANCELADA
            encargo.save(update_fields=["status"])
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


def juntar_resumo_do_credito(resumo, recebimento_credito):
    """Soma ao resumo do pagamento o que o crédito usado junto quitou (mesmo lançamento na tela)."""
    por_pk = {}
    for chave in ("quitadas", "parciais"):
        for cobranca, valor in resumo[chave]:
            por_pk[cobranca.pk] = [cobranca, valor]
    for aplicacao in recebimento_credito.aplicacoes.select_related("cobranca"):
        par = por_pk.setdefault(aplicacao.cobranca.pk, [aplicacao.cobranca, ZERO])
        par[1] += aplicacao.valor
    pares = sorted(por_pk.values(), key=lambda p: (p[0].vencimento, p[0].pk))
    resumo["quitadas"] = [(c, v) for c, v in pares if c.status == Cobranca.Status.PAGO]
    resumo["parciais"] = [(c, v) for c, v in pares if c.status != Cobranca.Status.PAGO]
    resumo["credito_usado"] = recebimento_credito.valor
    resumo["credito"] = MovimentoCredito.saldo_do_cliente(recebimento_credito.cliente)
    return resumo


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


def descricao_da_nd(numero, descricoes):
    """ "ND 004 — Combustível faltando +2 itens": quem lê a cobrança sabe do que se trata."""
    texto = f"ND {numero:03d}"
    if descricoes:
        texto += f" — {descricoes[0]}"
        if len(descricoes) > 1:
            outros = len(descricoes) - 1
            texto += f" +{outros} {'item' if outros == 1 else 'itens'}"
    maximo = Cobranca._meta.get_field("descricao").max_length
    return texto if len(texto) <= maximo else texto[: maximo - 1] + "…"


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
        observacoes=limpar_texto(observacoes),
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
        descricao=descricao_da_nd(nd.numero, [descricao for descricao, _ in itens]),
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
            data=data or timezone.localdate(),
            forma=forma,
        )
    return caucao


#: depósito da caução pode ter sido feito uns dias antes da entrega do carro
DIAS_DEPOSITO_ANTES_DO_CONTRATO = 30


def validar_data_caucao(caucao, tipo, data, hoje=None):
    """Movimentação de caução: nunca no futuro nem antes do contrato.

    O depósito (e o reforço) aceita até 30 dias antes da entrega — é comum o
    motorista depositar antes de pegar o carro.
    """
    hoje = hoje or timezone.localdate()
    if data.year < ANO_MINIMO_PAGAMENTO:
        raise ValidationError(f"Confira o ano da data ({data:%d/%m/%Y}).")
    if data > hoje:
        raise ValidationError("A data da movimentação não pode ser no futuro.")
    inicio = caucao.alocacao.data_inicio
    if tipo in (MovimentacaoCaucao.Tipo.RECEBIMENTO, MovimentacaoCaucao.Tipo.REFORCO):
        minimo = inicio - timedelta(days=DIAS_DEPOSITO_ANTES_DO_CONTRATO)
    else:
        minimo = inicio
    if data < minimo:
        raise ValidationError(
            f"A data {data:%d/%m/%Y} é antes do início do contrato ({inicio:%d/%m/%Y})."
        )


def _caucao_travada(caucao):
    """A caução relida com a linha travada — o saldo anotado na tela pode estar velho."""
    return (
        Caucao.objects.select_for_update(of=("self",))
        .select_related("alocacao__cliente", "alocacao__veiculo")
        .get(pk=caucao.pk)
    )


@transaction.atomic
def descontar_da_caucao(caucao, cobranca, valor, data, observacoes="", hoje=None):
    """Desconto na caução quitando (total ou parcialmente) uma cobrança.

    Trava cliente, caução e cobrança e relê os saldos: a tela pode estar velha
    (semana cancelada ao encerrar, ou paga noutra aba).
    """
    valor = Decimal(valor)
    if valor <= ZERO:
        raise ValidationError("O valor do desconto deve ser maior que zero.")
    Cliente.objects.select_for_update().filter(pk=cobranca.cliente_id).first()
    caucao = _caucao_travada(caucao)
    validar_data_caucao(caucao, MovimentacaoCaucao.Tipo.DESCONTO, data, hoje)
    cobranca = Cobranca.objects.select_for_update().get(pk=cobranca.pk)
    if cobranca.cliente_id != caucao.alocacao.cliente_id:
        raise ValidationError("Esta cobrança não é do cliente desta caução.")
    if cobranca.status not in Cobranca.STATUS_DEVIDOS:
        raise ValidationError(
            "Esta cobrança não está mais em aberto "
            f"(status: {cobranca.get_status_display()}) — recarregue a página."
        )
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
        observacoes=limpar_texto(observacoes),
    )
    cobranca.atualizar_status()
    atualizar_inadimplencia(cobranca.cliente)  # hoje, não a data do desconto
    return movimentacao


def pendencias_para_devolver(caucao):
    """O que pesa contra devolver a caução: (quanto o cliente deve, alocação ainda ativa?)."""
    cliente = caucao.alocacao.cliente
    devidas = Cobranca.com_quitacao_anotada(
        cliente.cobrancas.filter(status__in=Cobranca.STATUS_DEVIDOS)
    )
    deve = sum((c.saldo for c in devidas if c.saldo > ZERO), ZERO)
    return deve, caucao.alocacao.status == Alocacao.Status.ATIVA


@transaction.atomic
def registrar_movimentacao_caucao(
    caucao,
    tipo,
    valor,
    data,
    forma="",
    observacoes="",
    acima_do_combinado_confirmado=False,
    devolver_mesmo_assim=False,
    hoje=None,
):
    """Depósito, reforço ou devolução da caução (o desconto é descontar_da_caucao).

    Travas: forma da lista; data plausível; depósito/reforço acima do combinado
    só com confirmação (10.000 digitado no lugar de 1.000); devolução nunca
    acima do saldo e, com dívida em aberto ou carro ainda com o cliente, só com
    "devolver mesmo assim".
    """
    valor = Decimal(valor)
    if tipo not in (
        MovimentacaoCaucao.Tipo.RECEBIMENTO,
        MovimentacaoCaucao.Tipo.REFORCO,
        MovimentacaoCaucao.Tipo.DEVOLUCAO,
    ):
        raise ValidationError('Escolha em "O que aconteceu?" o tipo da movimentação.')
    if valor <= ZERO:
        raise ValidationError("O valor deve ser maior que zero.")
    Cliente.objects.select_for_update().filter(pk=caucao.alocacao.cliente_id).first()
    caucao = _caucao_travada(caucao)
    validar_data_caucao(caucao, tipo, data, hoje)
    if tipo == MovimentacaoCaucao.Tipo.DEVOLUCAO:
        forma = forma or ""
        if forma and forma not in Recebimento.Forma.values[:3]:
            raise ValidationError("Escolha a forma.")
        if valor > caucao.saldo:
            raise ValidationError(f"Devolução acima do saldo ({brl(caucao.saldo)}).")
        deve, ativa = pendencias_para_devolver(caucao)
        if (deve > ZERO or ativa) and not devolver_mesmo_assim:
            raise ValidationError(
                "Ainda há "
                + " e ".join(
                    p
                    for p in (
                        f"{brl(deve)} em aberto" if deve > ZERO else "",
                        f"o carro {caucao.alocacao.veiculo.placa} com o cliente" if ativa else "",
                    )
                    if p
                )
                + '. Para devolver mesmo assim, marque "Devolver mesmo assim".'
            )
    else:
        if forma not in Recebimento.Forma.values[:3]:
            raise ValidationError("Escolha a forma (Pix, dinheiro ou transferência).")
        combinado = caucao.alocacao.caucao_valor or ZERO
        if (
            combinado > ZERO
            and caucao.saldo + valor > combinado
            and not acima_do_combinado_confirmado
        ):
            raise ValidationError(
                f"Com este valor a caução fica em {brl(caucao.saldo + valor)}, acima do "
                f"combinado ({brl(combinado)}). Confira o valor; se estiver certo, marque "
                '"Confirmo: valor acima do combinado".'
            )
    return MovimentacaoCaucao.objects.create(
        caucao=caucao,
        tipo=tipo,
        valor=valor,
        data=data,
        forma=forma,
        observacoes=limpar_texto(observacoes),
    )


def pode_desfazer_movimentacao(movimentacao, hoje=None):
    """Como o recebimento: só no mesmo dia em que foi lançada (e não a sobra de um pagamento)."""
    hoje = hoje or timezone.localdate()
    if movimentacao.observacoes.startswith("Sobra do recebimento nº"):
        return False  # desfaz-se pelo recebimento que a gerou
    lancado_em = getattr(movimentacao, "lancado_em", ...)
    if lancado_em is ...:
        primeiro = movimentacao.history.filter(history_type="+").order_by("history_date").first()
        lancado_em = primeiro.history_date if primeiro else None
    return lancado_em is not None and timezone.localdate(lancado_em) == hoje


def anotar_lancamento_caucao(queryset):
    """Anota `lancado_em` nas movimentações de caução (para o "desfazer" do dia)."""
    historico = MovimentacaoCaucao.history.model.objects.filter(
        id=OuterRef("pk"), history_type="+"
    ).order_by("history_date")
    return queryset.annotate(lancado_em=Subquery(historico.values("history_date")[:1]))


@transaction.atomic
def desfazer_movimentacao_caucao(movimentacao, hoje=None):
    """Apaga uma movimentação de caução lançada hoje (valor ou tipo errado).

    Desconto desfeito devolve o saldo à cobrança (signals) e recalcula a
    inadimplência; depósito desfeito não pode deixar a caução negativa.
    """
    if not pode_desfazer_movimentacao(movimentacao, hoje):
        raise ValidationError(
            "Só dá para desfazer no mesmo dia em que a movimentação foi lançada. "
            "Para corrigir uma de outro dia, lance o acerto (ex.: uma devolução)."
        )
    caucao = _caucao_travada(movimentacao.caucao)
    movimentacao = MovimentacaoCaucao.objects.filter(pk=movimentacao.pk).first()
    if movimentacao is None:
        raise ValidationError("Esta movimentação já foi desfeita.")
    entrada = movimentacao.tipo in (
        MovimentacaoCaucao.Tipo.RECEBIMENTO,
        MovimentacaoCaucao.Tipo.REFORCO,
    )
    if entrada and movimentacao.valor > caucao.saldo:
        raise ValidationError(
            f"Os {brl(movimentacao.valor)} deste depósito já foram usados ou devolvidos — "
            "desfaça primeiro o desconto ou a devolução."
        )
    cliente = caucao.alocacao.cliente
    movimentacao.delete()  # signal: o desconto desfeito devolve o saldo à cobrança
    atualizar_inadimplencia(cliente)
    return movimentacao


def resumo_fiscal(ano, mes):
    """Base de cálculo do DAS do mês (decisão nº 11): só a receita de locação.

    Regime de caixa: aplicações pela data do recebimento e descontos de
    caução pela data da movimentação — abater da caução tem o mesmo efeito
    de quitação (docs.md §4.3).

    Sobra de pagamento que vira crédito do cliente (decisão D13): continua
    entrando na base só quando o crédito é usado, mas aparece no mês em que o
    dinheiro entrou como "adiantamentos recebidos" (fora da base), para a
    conta bater com o banco. `pago_com_credito` é a parte do mês quitada com
    crédito — já está nas linhas de cima, mas não é dinheiro novo no banco.
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
    descontado_da_caucao = ZERO
    for movimentacao in descontos:
        somar(movimentacao.cobranca, movimentacao.valor)
        descontado_da_caucao += movimentacao.valor
    caucao_recebida = (
        MovimentacaoCaucao.objects.filter(
            data__year=ano,
            data__month=mes,
            tipo__in=[MovimentacaoCaucao.Tipo.RECEBIMENTO, MovimentacaoCaucao.Tipo.REFORCO],
        ).aggregate(t=Sum("valor"))
    )["t"] or ZERO
    adiantamentos = (
        MovimentoCredito.objects.filter(
            tipo=MovimentoCredito.Tipo.ENTRADA, data__year=ano, data__month=mes
        )
        # sobra de um pagamento feito com crédito não é dinheiro novo
        .exclude(recebimento__forma=Recebimento.Forma.CREDITO)
        .aggregate(t=Sum("valor"))
    )["t"] or ZERO
    pago_com_credito = (
        AplicacaoRecebimento.objects.filter(
            recebimento__forma=Recebimento.Forma.CREDITO,
            recebimento__data__year=ano,
            recebimento__data__month=mes,
        ).aggregate(t=Sum("valor"))
    )["t"] or ZERO
    # sobra de pagamento feito com crédito que foi para a caução: está em
    # "caução recebida", mas não entrou dinheiro novo
    marcas_credito = [
        _marca_reforco(r)
        for r in Recebimento.objects.filter(
            forma=Recebimento.Forma.CREDITO, data__year=ano, data__month=mes
        ).only("pk")
    ]
    reforco_de_credito = (
        MovimentacaoCaucao.objects.filter(
            tipo=MovimentacaoCaucao.Tipo.REFORCO, observacoes__in=marcas_credito
        ).aggregate(t=Sum("valor"))["t"]
        or ZERO
        if marcas_credito
        else ZERO
    )
    total_diversos = sum(diversos.values(), ZERO)
    return {
        "locacao": locacao,
        "diversos": diversos,
        "total_diversos": total_diversos,
        "caucao_recebida": caucao_recebida,
        "adiantamentos": adiantamentos,
        "pago_com_credito": pago_com_credito,
        # o que deve bater com o banco/caixa: sem o uso de crédito nem o desconto
        # de caução (dinheiro que já tinha entrado antes)
        "entrou_no_caixa": locacao
        + total_diversos
        - pago_com_credito
        - descontado_da_caucao
        + adiantamentos
        + caucao_recebida
        - reforco_de_credito,
    }


def pagamentos_do_mes(ano, mes):
    """Cada pagamento que compõe o DAS do mês (2ª aba da planilha da contabilidade).

    Uma linha por aplicação de recebimento e por desconto de caução — a soma
    bate com "locação + pagamentos diversos" de resumo_fiscal.
    """
    linhas = []
    aplicacoes = AplicacaoRecebimento.objects.filter(
        recebimento__data__year=ano, recebimento__data__month=mes
    ).select_related("recebimento__cliente", "cobranca__alocacao__veiculo")
    for aplicacao in aplicacoes:
        cobranca = aplicacao.cobranca
        linhas.append(
            {
                "data": aplicacao.recebimento.data,
                "cliente": aplicacao.recebimento.cliente.nome,
                "placa": cobranca.alocacao.veiculo.placa if cobranca.alocacao_id else "",
                "descricao": cobranca.descricao,
                "forma": aplicacao.recebimento.get_forma_display(),
                "valor": aplicacao.valor,
                "entra_no_das": cobranca.classificacao_fiscal == "locacao",
            }
        )
    descontos = MovimentacaoCaucao.objects.filter(
        tipo=MovimentacaoCaucao.Tipo.DESCONTO,
        data__year=ano,
        data__month=mes,
        cobranca__isnull=False,
    ).select_related("cobranca__cliente", "cobranca__alocacao__veiculo")
    for movimentacao in descontos:
        cobranca = movimentacao.cobranca
        linhas.append(
            {
                "data": movimentacao.data,
                "cliente": cobranca.cliente.nome,
                "placa": cobranca.alocacao.veiculo.placa if cobranca.alocacao_id else "",
                "descricao": cobranca.descricao,
                "forma": "Desconto na caução",
                "valor": movimentacao.valor,
                "entra_no_das": cobranca.classificacao_fiscal == "locacao",
            }
        )
    linhas.sort(key=lambda linha: (linha["data"], linha["cliente"]))
    return linhas
