"""Vigências e documentos a vencer — rastreador, bateria e CNH (docs.md §5)."""

from datetime import date, timedelta

from apps.pessoas.models import Cliente

from .models import Veiculo

DIAS_ALERTA_VIGENCIA = 30


def vigencias_a_vencer(hoje=None):
    hoje = hoje or date.today()
    limite = hoje + timedelta(days=DIAS_ALERTA_VIGENCIA)
    alertas = []
    frota = Veiculo.objects.exclude(status=Veiculo.Status.VENDIDO)
    for veiculo in frota.filter(rastreador_vigencia_fim__lte=limite):
        alertas.append(
            {
                "data": veiculo.rastreador_vigencia_fim,
                "vencido": veiculo.rastreador_vigencia_fim < hoje,
                "descricao": f"Rastreador do {veiculo.placa}"
                + (f" ({veiculo.rastreador_fornecedor})" if veiculo.rastreador_fornecedor else ""),
            }
        )
    for veiculo in frota.filter(bateria_garantia_fim__lte=limite):
        alertas.append(
            {
                "data": veiculo.bateria_garantia_fim,
                "vencido": veiculo.bateria_garantia_fim < hoje,
                "descricao": f"Garantia da bateria do {veiculo.placa}",
            }
        )
    for cliente in Cliente.objects.filter(
        status__in=[Cliente.Status.ATIVO, Cliente.Status.INADIMPLENTE],
        cnh_validade__lte=limite,
    ):
        alertas.append(
            {
                "data": cliente.cnh_validade,
                "vencido": cliente.cnh_validade < hoje,
                "descricao": f"CNH de {cliente.nome}",
            }
        )
    return sorted(alertas, key=lambda a: a["data"])
