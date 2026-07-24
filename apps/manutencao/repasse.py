"""Repasse de manutenção ao cliente (docs.md §4.5)."""

from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.alocacoes.services import cliente_vigente
from apps.financeiro.models import Cobranca


@transaction.atomic
def gerar_repasse(manutencao, vencimento=None):
    """Cria a cobrança de repasse quando o custo é do cliente (valor cobrado ≠ custo real)."""
    if manutencao.responsavel != manutencao.Responsavel.CLIENTE:
        raise ValidationError("O responsável pelo custo não é o cliente.")
    if not manutencao.valor_cobrado_cliente:
        raise ValidationError("Informe o valor cobrado do cliente.")
    if manutencao.cobranca_repasse:
        raise ValidationError("Repasse já gerado para esta manutenção.")
    cliente = cliente_vigente(manutencao.veiculo, manutencao.data)
    if not cliente:
        raise ValidationError(
            "Nenhum cliente estava com o veículo na data — repasse manual pelo financeiro."
        )
    cobranca = Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.REPASSE_MANUTENCAO,
        descricao=f"Repasse manutenção {manutencao.veiculo.placa} — {manutencao.descricao[:80]}",
        valor=manutencao.valor_cobrado_cliente,
        vencimento=vencimento or date.today(),
    )
    manutencao.cobranca_repasse = cobranca
    manutencao.save(update_fields=["cobranca_repasse"])
    return cobranca
