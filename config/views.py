from datetime import date

from django.shortcuts import render

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.financeiro import reports
from apps.frota.models import Veiculo
from apps.km.models import veiculos_com_leitura_pendente
from apps.manutencao.services import preventivas_em_alerta
from apps.multas.services import multas_com_fici_a_vencer
from apps.pessoas.models import Cliente
from apps.sinistros.models import Sinistro
from apps.sinistros.services import sinistros_com_auxilio_a_solicitar


def painel(request):
    """Painel inicial — frota, leituras pendentes e preventivas em alerta (docs.md §5)."""
    veiculos = Veiculo.objects.exclude(status=Veiculo.Status.VENDIDO)
    alertas_preventivas = preventivas_em_alerta()
    contexto = {
        "total_veiculos": veiculos.count(),
        "veiculos_por_status": {
            label: veiculos.filter(status=valor).count()
            for valor, label in Veiculo.Status.choices
            if valor != Veiculo.Status.VENDIDO
        },
        "total_clientes_ativos": Cliente.objects.filter(status=Cliente.Status.ATIVO).count(),
        "inadimplentes": Cliente.objects.filter(status=Cliente.Status.INADIMPLENTE),
        "total_a_receber": reports.total_a_receber(),
        "alocacoes_ativas": Alocacao.objects.filter(status=Alocacao.Status.ATIVA).count(),
        "trocas_em_andamento": TrocaTemporaria.objects.filter(
            data_devolucao__isnull=True
        ).select_related("alocacao__cliente", "veiculo_substituto"),
        "leituras_pendentes": veiculos_com_leitura_pendente(date.today()),
        "alertas_preventivas": alertas_preventivas,
        "total_itens_em_alerta": sum(len(itens) for _, itens in alertas_preventivas),
        "fici_a_vencer": multas_com_fici_a_vencer(),
        "auxilios_a_solicitar": sinistros_com_auxilio_a_solicitar(),
        "sinistros_abertos": Sinistro.objects.filter(
            status__in=[Sinistro.Status.ABERTO, Sinistro.Status.REGULARIZACAO]
        ).count(),
    }
    return render(request, "painel.html", contexto)
