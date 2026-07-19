from django.shortcuts import render

from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente


def painel(request):
    """Painel inicial — versão fundação; será expandido nas próximas etapas (docs.md §5)."""
    veiculos = Veiculo.objects.exclude(status=Veiculo.Status.VENDIDO)
    contexto = {
        "total_veiculos": veiculos.count(),
        "veiculos_por_status": {
            label: veiculos.filter(status=valor).count()
            for valor, label in Veiculo.Status.choices
            if valor != Veiculo.Status.VENDIDO
        },
        "total_clientes_ativos": Cliente.objects.filter(status=Cliente.Status.ATIVO).count(),
    }
    return render(request, "painel.html", contexto)
