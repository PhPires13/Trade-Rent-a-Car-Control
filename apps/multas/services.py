"""Regras de multas: NIC, ND de multas e alertas de FICI (docs.md §4.7)."""

from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.financeiro.models import Cobranca, ItemNotaDebito, NotaDebito

from .models import Multa

DIAS_ALERTA_FICI = 7


@transaction.atomic
def registrar_nic(multa, data_infracao=None, valor=None):
    """FICI perdido → multa NIC contra a empresa, vinculada à original (docs.md §4.7)."""
    if hasattr(multa, "multa_nic"):
        raise ValidationError("Esta multa já tem NIC registrada.")
    multa.fici_status = Multa.Fici.PRAZO_PERDIDO
    multa.save(update_fields=["fici_status"])
    return Multa.objects.create(
        veiculo=multa.veiculo,
        cliente=multa.cliente,
        data_infracao=data_infracao or multa.data_infracao,
        orgao=multa.orgao,
        descricao="Multa por não indicação de condutor (NIC)",
        valor=valor,
        responsavel=Multa.Responsavel.EMPRESA,
        tipo_condutor=Multa.TipoCondutor.EMPRESA,
        fici_status=Multa.Fici.NAO_SE_APLICA,
        multa_origem_nic=multa,
    )


@transaction.atomic
def gerar_nd_de_multas(cliente, multas, data_emissao, vencimento=None):
    """Agrupa multas 'a cobrar' do cliente numa ND numerada (docs.md §4.3/4.7)."""
    multas = list(multas)
    if not multas:
        raise ValidationError("Selecione ao menos uma multa.")
    for multa in multas:
        if multa.cliente_id != cliente.pk:
            raise ValidationError(f"{multa} não é do cliente {cliente.nome}.")
        if multa.repasse != "A cobrar":
            raise ValidationError(f"{multa} não está 'a cobrar' (situação: {multa.repasse}).")
    nd = NotaDebito(cliente=cliente, data_emissao=data_emissao, vencimento=vencimento, numero=None)
    nd.save()
    for multa in multas:
        ItemNotaDebito.objects.create(
            nota_debito=nd,
            descricao=f"Multa {multa.codigo or ''} {multa.descricao} "
            f"({multa.data_infracao:%d/%m/%Y})".strip(),
            valor=multa.valor,
            multa=multa,
        )
    Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.NOTA_DEBITO,
        descricao=f"Nota de débito ND {nd.numero:03d} (multas)",
        valor=nd.total,
        vencimento=vencimento or data_emissao,
        nota_debito=nd,
    )
    return nd


def alertas_fici(hoje=None):
    """Multas com FICI pendente vencendo em até 7 dias ou já vencido."""
    hoje = hoje or date.today()
    limite = hoje + timedelta(days=DIAS_ALERTA_FICI)
    return (
        Multa.objects.filter(fici_status=Multa.Fici.PENDENTE, fici_prazo__lte=limite)
        .select_related("veiculo", "cliente")
        .order_by("fici_prazo")
    )
