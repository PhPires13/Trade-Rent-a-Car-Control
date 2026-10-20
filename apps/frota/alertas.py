"""Vigências e documentos a vencer — IPVA, licenciamento, rastreador, bateria e CNH (§5)."""

from datetime import date, timedelta

from apps.formatos import brl
from apps.pessoas.models import Cliente

from .models import Veiculo

DIAS_ALERTA_VIGENCIA = 30


def _alerta(data, hoje, descricao):
    return {"data": data, "vencido": data < hoje, "descricao": descricao}


def vigencias_a_vencer(hoje=None):
    """Alertas vencidos ou vencendo nos próximos 30 dias, do mais antigo ao mais novo."""
    hoje = hoje or date.today()
    limite = hoje + timedelta(days=DIAS_ALERTA_VIGENCIA)
    alertas = []
    frota = Veiculo.objects.exclude(status=Veiculo.Status.VENDIDO)
    # IPVA em aberto (sem data de pagamento) vencendo — decisão nº 21 / tabela do §5
    for veiculo in frota.filter(ipva_vencimento__lte=limite, ipva_pago_em__isnull=True):
        alertas.append(
            _alerta(
                veiculo.ipva_vencimento,
                hoje,
                f"IPVA do {veiculo.placa}"
                + (f" ({veiculo.ipva_ano})" if veiculo.ipva_ano else "")
                + (f" — {brl(veiculo.ipva_valor)}" if veiculo.ipva_valor else ""),
            )
        )
    for veiculo in frota.filter(licenciamento_vencimento__lte=limite):
        alertas.append(
            _alerta(veiculo.licenciamento_vencimento, hoje, f"Licenciamento do {veiculo.placa}")
        )
    for veiculo in frota.filter(rastreador_vigencia_fim__lte=limite):
        alertas.append(
            _alerta(
                veiculo.rastreador_vigencia_fim,
                hoje,
                f"Rastreador do {veiculo.placa}"
                + (f" ({veiculo.rastreador_fornecedor})" if veiculo.rastreador_fornecedor else ""),
            )
        )
    for veiculo in frota.filter(bateria_garantia_fim__lte=limite):
        alertas.append(
            _alerta(veiculo.bateria_garantia_fim, hoje, f"Garantia da bateria do {veiculo.placa}")
        )
    for cliente in Cliente.objects.filter(
        status__in=[Cliente.Status.ATIVO, Cliente.Status.INADIMPLENTE],
        cnh_validade__lte=limite,
    ):
        alertas.append(_alerta(cliente.cnh_validade, hoje, f"CNH de {cliente.nome}"))
    return sorted(alertas, key=lambda a: a["data"])
