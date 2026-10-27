"""Regras de multas: NIC, ND de multas e alertas de FICI (docs.md §4.7)."""

from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Exists, F, OuterRef, Q
from django.utils import timezone

from apps.financeiro.models import Cobranca, ItemNotaDebito, NotaDebito
from apps.financeiro.services import proximo_dia_pagamento
from apps.formatos import brl
from apps.pessoas.models import Cliente

from .models import Multa

DIAS_ALERTA_FICI = 7


@transaction.atomic
def registrar_nic(multa, data_infracao=None, valor=None):
    """FICI perdido → multa NIC contra a empresa, vinculada à original (docs.md §4.7)."""
    # trava a multa: dois envios ao mesmo tempo esperam um pelo outro e o segundo
    # recebe o aviso abaixo (antes, o banco recusava a 2ª NIC com erro 500)
    atual = Multa.objects.select_for_update().filter(pk=multa.pk).values("fici_status", "resultado")
    atual = atual.first() or {"fici_status": multa.fici_status, "resultado": multa.resultado}
    if Multa.objects.filter(multa_origem_nic_id=multa.pk).exists():
        raise ValidationError("Esta multa já tem NIC registrada.")
    if atual["resultado"] in Multa.RESULTADOS_ANULADORES:
        raise ValidationError("Esta multa foi anulada (cancelada, advertência ou não exigível).")
    if atual["fici_status"] not in (Multa.Fici.PENDENTE, Multa.Fici.PRAZO_PERDIDO):
        # motorista já indicado, ou multa que já é uma NIC: não há NIC a registrar
        raise ValidationError("Não há indicação de motorista pendente nesta multa.")
    multa.fici_status = Multa.Fici.PRAZO_PERDIDO
    multa.save(update_fields=["fici_status"])
    nic = Multa.objects.create(
        veiculo=multa.veiculo,
        cliente=multa.cliente,
        # a NIC é uma multa nova: entra na data em que foi registrada, não na da
        # infração original — o mês dela pode já ter ido para a contabilidade
        data_infracao=data_infracao or timezone.localdate(),
        orgao=multa.orgao,
        descricao="Multa por não indicação de condutor (NIC)",
        valor=valor,
        responsavel=Multa.Responsavel.EMPRESA,
        tipo_condutor=Multa.TipoCondutor.EMPRESA,
        fici_status=Multa.Fici.NAO_SE_APLICA,
        multa_origem_nic=multa,
    )
    if multa.cliente_id is None and nic.cliente_id is not None:
        # o save() buscou quem está com o carro na data da NIC (hoje) — outro
        # motorista, sem relação com a infração original: a NIC fica sem cliente
        Multa.objects.filter(pk=nic.pk).update(cliente=None)
        nic.cliente = None
    return nic


def _travar_cliente(cliente):
    """Trava a linha do cliente até o fim da transação (como registrar_recebimento):
    o 2º pedido de ND do mesmo cliente espera o 1º gravar e então vê a multa na ND."""
    Cliente.objects.select_for_update().filter(pk=cliente.pk).first()


@transaction.atomic
def gerar_nd_de_multas(cliente, multas, data_emissao, vencimento=None):
    """Agrupa multas 'a cobrar' do cliente numa ND numerada (docs.md §4.3/4.7)."""
    multas = list(multas)
    if not multas:
        raise ValidationError("Selecione ao menos uma multa.")
    _travar_cliente(cliente)
    # relê do banco depois da trava: dois "Gerar ND" ao mesmo tempo (clique duplo)
    # viam as duas a multa "A cobrar" e ela entrava em duas NDs
    pks = [multa.pk for multa in multas]
    multas = list(Multa.objects.select_related("veiculo").filter(pk__in=pks).order_by("pk"))
    for multa in multas:
        if multa.cliente_id != cliente.pk:
            raise ValidationError(f"{multa} não é do cliente {cliente.nome}.")
        if multa.repasse != "A cobrar":
            raise ValidationError(f"{multa} não está 'a cobrar' (situação: {multa.repasse}).")
    if vencimento is None:
        # mesma regra da ND do financeiro: vence no próximo dia de pagamento do
        # cliente — nunca no dia da emissão, senão amanhã ele já fica inadimplente
        vencimento = proximo_dia_pagamento(cliente, data_emissao)
    nd = NotaDebito(cliente=cliente, data_emissao=data_emissao, vencimento=vencimento, numero=None)
    nd.save()
    for multa in multas:
        ItemNotaDebito.objects.create(
            nota_debito=nd,
            # só as partes que existem: multa sem código ficava "Multa  descrição"
            descricao=" ".join(
                parte.strip()
                for parte in ("Multa", multa.codigo or "", multa.descricao or "")
                if parte and parte.strip()
            )
            + f" ({multa.data_infracao:%d/%m/%Y})",
            valor=multa.valor,
            multa=multa,
        )
    Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.NOTA_DEBITO,
        descricao=f"Nota de débito ND {nd.numero:03d} (multas)",
        valor=nd.total,
        vencimento=vencimento,
        nota_debito=nd,
    )
    return nd


def alertas_fici(hoje=None):
    """Multas com FICI pendente vencendo em até 7 dias, já vencido ou sem prazo.

    Sem prazo informado também entra (no topo): é o caso mais arriscado — o
    prazo real pode passar sem ninguém ver e virar NIC. O painel mostra
    "prazo não informado" nessas.
    """
    hoje = hoje or timezone.localdate()
    limite = hoje + timedelta(days=DIAS_ALERTA_FICI)
    return (
        Multa.objects.fici_pendente()
        .filter(Q(fici_prazo__lte=limite) | Q(fici_prazo__isnull=True))
        .select_related("veiculo", "cliente")
        .order_by(F("fici_prazo").asc(nulls_first=True), "-data_infracao")
    )


# --- Atalhos da lista: as tarefas reais de quem cuida das multas (auditoria UX #11) ---

#: ordem e rótulo dos botões-atalho no topo da lista
TAREFAS = (
    ("indicar", "Indicar condutor"),
    ("pagar", "Pagar ao órgão"),
    ("cobrar", "Cobrar do cliente"),
    ("definir", "Definir responsável"),
    ("concluidas", "Concluídas"),
)


def _filtros_tarefa():
    """Q de cada tarefa — espera o queryset anotado por `com_tarefas`.

    "Cobrar do cliente" espelha `Multa.repasse == "A cobrar"` no banco: sem ND,
    responsável cliente/condutor, com valor, resultado que não anulou a multa e
    que não foi paga pelo próprio cliente direto ao órgão — e com cliente (sem
    cliente não dá para gerar ND).
    "Definir responsável" é o mesmo caso sem cliente (`repasse` = "Sem cliente").
    "Concluídas" é o que não está em nenhuma das outras.
    """
    anulada = Q(resultado__in=Multa.RESULTADOS_ANULADORES)
    indicar = Q(fici_status=Multa.Fici.PENDENTE) & ~anulada
    # sem valor ainda (a carta com o valor chega depois) também é tarefa: antes a
    # multa sumia das tarefas e caía em "Concluídas" sem ter sido paga
    pagar = (
        Q(pagamento=Multa.Pagamento.PENDENTE) & (Q(valor__gt=0) | Q(valor__isnull=True)) & ~anulada
    )
    paga_pelo_cliente = Q(pagamento=Multa.Pagamento.PAGO, pago_por__iexact=Multa.PAGO_POR_CLIENTE)
    devida_pelo_motorista = (
        Q(_tem_nd=False, valor__gt=0)
        & Q(responsavel__in=[Multa.Responsavel.CLIENTE, Multa.Responsavel.CONDUTOR])
        & ~anulada
        & ~paga_pelo_cliente
    )
    cobrar = devida_pelo_motorista & Q(cliente__isnull=False)
    definir = devida_pelo_motorista & Q(cliente__isnull=True)
    return {
        "indicar": indicar,
        "pagar": pagar,
        "cobrar": cobrar,
        "definir": definir,
        "concluidas": ~indicar & ~pagar & ~cobrar & ~definir,
    }


def com_tarefas(multas):
    """Anota o que os filtros de tarefa precisam (se a multa já entrou em ND)."""
    return multas.annotate(_tem_nd=Exists(ItemNotaDebito.objects.filter(multa=OuterRef("pk"))))


def filtrar_tarefa(multas, tarefa):
    """Aplica o atalho; "indicar" vem pelo prazo mais próximo (sem prazo por último)."""
    filtros = _filtros_tarefa()
    if tarefa not in filtros:
        return multas
    multas = com_tarefas(multas).filter(filtros[tarefa])
    if tarefa == "indicar":
        multas = multas.order_by(F("fici_prazo").asc(nulls_last=True), "-data_infracao")
    return multas


def contar_tarefas(multas, hoje=None):
    """Contadores dos botões-atalho numa consulta só, mais as de prazo já vencido."""
    hoje = hoje or timezone.localdate()
    filtros = _filtros_tarefa()
    contas = {nome: Count("pk", filter=filtro) for nome, filtro in filtros.items()}
    contas["indicar_vencidas"] = Count("pk", filter=filtros["indicar"] & Q(fici_prazo__lt=hoje))
    return com_tarefas(multas).aggregate(**contas)


# --- Histórico simples da página de editar (auditoria UX #10) ---


def _texto_do_valor(campo, registro, valor):
    """Valor de um campo do histórico em texto de gente ("Pago", "R$ 195,30", "05/09/2026")."""
    if valor in (None, ""):
        return "vazio"
    if campo.choices:
        return str(dict(campo.flatchoices).get(valor, valor))
    if campo.is_relation:
        try:
            return str(getattr(registro, campo.name))
        except campo.related_model.DoesNotExist:
            return "(apagado)"
    if isinstance(valor, date):
        return f"{valor:%d/%m/%Y}"
    if campo.get_internal_type() == "DecimalField":
        return brl(valor)
    return str(valor)


def historico(multa, limite=20):
    """Quem mudou o quê e quando — do mais recente para o mais antigo.

    Lê os registros do simple_history e devolve só o que mudou de um para o
    outro, com o nome do campo e os valores em texto comum.
    """
    registros = list(multa.history.select_related("history_user")[: limite + 1])
    eventos = []
    for atual, anterior in zip(registros, registros[1:] + [None], strict=True):
        if len(eventos) == limite:
            break
        mudancas = []
        if anterior is not None:
            for mudanca in atual.diff_against(anterior).changes:
                campo = Multa._meta.get_field(mudanca.field)
                mudancas.append(
                    {
                        "campo": str(campo.verbose_name).capitalize(),
                        "antes": _texto_do_valor(campo, anterior, mudanca.old),
                        "depois": _texto_do_valor(campo, atual, mudanca.new),
                    }
                )
            if not mudancas:
                continue  # salvou sem mudar nada
        usuario = atual.history_user
        eventos.append(
            {
                "quando": atual.history_date,
                "quem": (usuario.get_full_name() or usuario.username) if usuario else "",
                "criacao": atual.history_type == "+",
                "mudancas": mudancas,
            }
        )
    return eventos
