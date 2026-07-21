"""Regras de sinistros e auxílio motorista (docs.md §4.6)."""

from datetime import date

from apps.alocacoes.services import cliente_vigente
from apps.manutencao.models import Manutencao

from .models import DIAS_PARA_AUXILIO, AuxilioMotorista, Sinistro


def preencher_motorista(sinistro):
    """Preenche o motorista vigente na data do sinistro (docs.md §4.6)."""
    if sinistro.motorista_id is None and sinistro.veiculo_id and sinistro.data:
        sinistro.motorista = cliente_vigente(sinistro.veiculo, sinistro.data)
    return sinistro.motorista


def sinistros_com_auxilio_a_solicitar(hoje=None):
    """Colisões cujo veículo está parado > 7 dias e ainda não têm auxílio (docs.md §4.6).

    Usa as manutenções do veículo abertas (sem saída) a partir da data do sinistro.
    """
    hoje = hoje or date.today()
    candidatos = []
    colisoes = (
        Sinistro.objects.filter(tipo=Sinistro.Tipo.COLISAO)
        .exclude(status=Sinistro.Status.CONCLUIDO)
        .select_related("veiculo")
    )
    for sinistro in colisoes:
        if sinistro.auxilios.exists():
            continue
        manutencao = (
            Manutencao.objects.filter(
                veiculo=sinistro.veiculo,
                data_entrada__isnull=False,
                data_entrada__gte=sinistro.data,
            )
            .order_by("data_entrada")
            .first()
        )
        if not manutencao or not manutencao.data_entrada:
            continue
        fim = manutencao.data_saida or hoje
        dias = (fim - manutencao.data_entrada).days
        if dias > DIAS_PARA_AUXILIO:
            candidatos.append((sinistro, dias))
    return candidatos


def registrar_auxilio(sinistro, dias, valor=None):
    """Cria o registro de auxílio a solicitar (docs.md §4.6)."""
    return AuxilioMotorista.objects.create(
        sinistro=sinistro,
        dias_parado=dias,
        valor=valor,
        status=AuxilioMotorista.Status.A_SOLICITAR,
    )
