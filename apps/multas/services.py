"""Regras de multas (docs.md §4.7)."""

from datetime import date, timedelta

from django.db import transaction

from apps.alocacoes.services import cliente_vigente
from apps.financeiro.models import Cobranca, ItemNotaDebito, NotaDebito

from .models import Multa

# Aviso de FICI com esta antecedência do prazo.
DIAS_ALERTA_FICI = 15


def preencher_cliente_alocacao(multa):
    """Preenche o cliente que estava com o carro na data (docs.md §4.7)."""
    if multa.cliente_alocacao_id is None and multa.veiculo_id and multa.data:
        multa.cliente_alocacao = cliente_vigente(multa.veiculo, multa.data)
    return multa.cliente_alocacao


def multas_com_fici_a_vencer(hoje=None):
    """Multas com indicação pendente e prazo se aproximando (evita multa NIC)."""
    hoje = hoje or date.today()
    limite = hoje + timedelta(days=DIAS_ALERTA_FICI)
    return (
        Multa.objects.filter(
            fici_status=Multa.FICI.PENDENTE,
            fici_prazo__isnull=False,
            fici_prazo__lte=limite,
        )
        .select_related("veiculo", "cliente_alocacao", "orgao")
        .order_by("fici_prazo")
    )


@transaction.atomic
def emitir_nota_debito(cliente, multas, data_emissao=None):
    """Agrupa multas 'a cobrar' numa ND e gera a cobrança (docs.md §4.3/§4.7)."""
    data_emissao = data_emissao or date.today()
    multas = [m for m in multas if m.repasse == Multa.Repasse.A_COBRAR and m.valor]
    if not multas:
        return None

    nota = NotaDebito.objects.create(cliente=cliente, data_emissao=data_emissao)
    total = 0
    for multa in multas:
        ItemNotaDebito.objects.create(
            nota=nota,
            descricao=f"Multa {multa.codigo or ''} {multa.descricao} "
            f"({multa.data:%d/%m/%Y})".strip(),
            valor=multa.valor,
        )
        total += multa.valor
        multa.repasse = Multa.Repasse.INCLUIDA_ND
        multa.save(update_fields=["repasse"])

    cobranca = Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.NOTA_DEBITO,
        descricao=f"ND {nota.numero:03d}",
        valor=total,
        vencimento=data_emissao + timedelta(days=7),
    )
    nota.cobranca = cobranca
    nota.save(update_fields=["cobranca"])
    return nota
