from datetime import date

from django.shortcuts import render

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.frota.models import Veiculo
from apps.km.models import veiculos_com_leitura_pendente
from apps.manutencao.services import preventivas_em_alerta
from apps.pessoas.models import Cliente


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
        "alocacoes_ativas": Alocacao.objects.filter(status=Alocacao.Status.ATIVA).count(),
        "trocas_em_andamento": TrocaTemporaria.objects.filter(
            data_devolucao__isnull=True
        ).select_related("alocacao__cliente", "veiculo_substituto"),
        "leituras_pendentes": veiculos_com_leitura_pendente(date.today()),
        "alertas_preventivas": alertas_preventivas,
        "total_itens_em_alerta": sum(len(itens) for _, itens in alertas_preventivas),
    }
    return render(request, "painel.html", contexto)
