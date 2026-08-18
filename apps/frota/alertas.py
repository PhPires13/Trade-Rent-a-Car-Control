"""Vigências e documentos a vencer — IPVA, licenciamento, rastreador, bateria e CNH (§5)."""

from datetime import date, timedelta

from apps.pessoas.models import Cliente

from .models import Veiculo

DIAS_ALERTA_VIGENCIA = 30


def vigencias_a_vencer(hoje=None):
    hoje = hoje or date.today()
    limite = hoje + timedelta(days=DIAS_ALERTA_VIGENCIA)
    alertas = []
    frota = Veiculo.objects.exclude(status=Veiculo.Status.VENDIDO)
    # IPVA em aberto (sem data de pagamento) vencendo — decisão nº 21 / tabela do §5
    for veiculo in frota.filter(ipva_vencimento__lte=limite, ipva_pago_em__isnull=True):
        alertas.append(
            {
                "data": veiculo.ipva_vencimento,
                "vencido": veiculo.ipva_vencimento < hoje,
                "descricao": f"IPVA do {veiculo.placa}"
                + (f" ({veiculo.ipva_ano})" if veiculo.ipva_ano else "")
                + (f" — R$ {veiculo.ipva_valor}" if veiculo.ipva_valor else ""),
            }
        )
    for veiculo in frota.filter(licenciamento_vencimento__lte=limite):
        alertas.append(
            {
                "data": veiculo.licenciamento_vencimento,
                "vencido": veiculo.licenciamento_vencimento < hoje,
                "descricao": f"Licenciamento do {veiculo.placa}",
            }
        )
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
