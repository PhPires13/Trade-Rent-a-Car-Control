from datetime import date, timedelta

from django.conf import settings
from django.db.models import Q, Sum
from django.shortcuts import redirect, render
from django.views.static import serve

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.financeiro.models import ZERO, Cobranca
from apps.frota.models import Veiculo, normalizar_placa
from apps.km.models import veiculos_com_leitura_pendente
from apps.manutencao.services import preventivas_em_alerta
from apps.multas.services import alertas_fici
from apps.pessoas.models import Cliente
from apps.sinistros.models import Sinistro


def midia(request, caminho):
    """Serve fotos e documentos enviados — SEMPRE atrás do login.

    O LoginRequiredMiddleware cobre esta view como qualquer outra; CNH e fotos
    de clientes nunca podem virar URL pública (por isso não usamos static()).
    """
    return serve(request, caminho, document_root=settings.MEDIA_ROOT)


def buscar(request):
    """Busca global por placa ou nome de cliente (barra do topo)."""
    consulta = request.GET.get("q", "").strip()
    veiculos, clientes = [], []
    if consulta:
        placa = normalizar_placa(consulta)
        veiculos = list(
            Veiculo.objects.filter(
                Q(placa__icontains=placa) | Q(marca_modelo__icontains=consulta)
            ).order_by("placa")[:20]
        )
        clientes = list(Cliente.objects.filter(nome__icontains=consulta).order_by("nome")[:20])
        if len(veiculos) == 1 and not clientes:
            return redirect("frota:detalhe", veiculos[0].pk)
        if len(clientes) == 1 and not veiculos:
            return redirect("pessoas:detalhe", clientes[0].pk)
    return render(
        request,
        "buscar.html",
        {"consulta": consulta, "veiculos": veiculos, "clientes": clientes},
    )


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
    hoje = date.today()
    semana = (hoje - timedelta(days=hoje.weekday()), hoje + timedelta(days=6 - hoje.weekday()))
    da_semana = Cobranca.objects.filter(vencimento__range=semana).exclude(
        status=Cobranca.Status.CANCELADA
    )
    atrasadas = Cobranca.objects.filter(status=Cobranca.Status.ATRASADO)
    contexto.update(
        {
            "semana_total": da_semana.aggregate(t=Sum("valor"))["t"] or ZERO,
            "semana_recebido": sum((c.total_quitado for c in da_semana), ZERO),
            "atrasado_total": sum((c.saldo for c in atrasadas), ZERO),
            "inadimplentes": Cliente.objects.filter(status=Cliente.Status.INADIMPLENTE),
            "hoje": hoje,
            "fici_em_alerta": alertas_fici(hoje),
            # prefetch das manutenções: auxilio_disponivel lê dias_parado de cada
            # sinistro logo abaixo, o que consultava uma vez por sinistro aberto
            "sinistros_abertos": Sinistro.objects.exclude(status=Sinistro.Status.CONCLUIDO)
            .select_related("veiculo")
            .prefetch_related("manutencoes"),
        }
    )
    contexto["auxilios_disponiveis"] = [
        s for s in contexto["sinistros_abertos"] if s.auxilio_disponivel
    ]
    from apps.frota.alertas import vigencias_a_vencer
    from apps.frota.desmobilizacao import ranking_da_frota

    fichas, _ = ranking_da_frota(hoje)
    contexto["candidatos_venda"] = [f for f in fichas if f.nivel in ("preparar", "vender")]
    contexto["vigencias"] = vigencias_a_vencer(hoje)
    frota_locacao = veiculos.filter(uso=Veiculo.Uso.LOCACAO)
    alugados = frota_locacao.filter(status=Veiculo.Status.ALOCADO).count()
    total_locacao = frota_locacao.count()
    contexto["ocupacao"] = round(100 * alugados / total_locacao) if total_locacao else None
    contexto["nds_abertas"] = Cobranca.objects.filter(
        origem=Cobranca.Origem.NOTA_DEBITO,
        status__in=[
            Cobranca.Status.PENDENTE,
            Cobranca.Status.PARCIAL,
            Cobranca.Status.ATRASADO,
        ],
    ).count()
    return render(request, "painel.html", contexto)
