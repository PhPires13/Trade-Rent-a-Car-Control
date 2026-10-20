from datetime import date, timedelta

from django.conf import settings
from django.db.models import Count, Q
from django.shortcuts import redirect, render
from django.views.static import serve

from apps.alocacoes.models import TrocaTemporaria
from apps.financeiro.models import ZERO, Cobranca
from apps.frota.alertas import vigencias_a_vencer
from apps.frota.desmobilizacao import ranking_da_frota
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
        filtro = Q(marca_modelo__icontains=consulta)
        # "-" ou " - " viram placa vazia, e placa__icontains="" acharia a frota inteira
        if placa := normalizar_placa(consulta):
            filtro |= Q(placa__icontains=placa)
        veiculos = list(Veiculo.objects.filter(filtro).order_by("placa")[:20])
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
    """Painel inicial — dinheiro, frota e alertas do dia (docs.md §5).

    Cada bloco consulta em lote: o número de queries não acompanha o tamanho
    da frota nem o de cobranças (config/tests.py mede os dois).
    """
    hoje = date.today()
    veiculos = Veiculo.objects.exclude(status=Veiculo.Status.VENDIDO)
    status_no_painel = [
        (valor, rotulo)
        for valor, rotulo in Veiculo.Status.choices
        if valor != Veiculo.Status.VENDIDO
    ]
    locacao = Q(uso=Veiculo.Uso.LOCACAO)
    contagem = veiculos.aggregate(
        total=Count("pk"),
        locacao=Count("pk", filter=locacao),
        alugados=Count("pk", filter=locacao & Q(status=Veiculo.Status.ALOCADO)),
        **{f"status_{valor}": Count("pk", filter=Q(status=valor)) for valor, _ in status_no_painel},
    )

    semana = (hoje - timedelta(days=hoje.weekday()), hoje + timedelta(days=6 - hoje.weekday()))
    da_semana = list(
        Cobranca.com_quitacao_anotada(
            Cobranca.objects.filter(vencimento__range=semana).exclude(
                status=Cobranca.Status.CANCELADA
            )
        )
    )
    atrasadas = Cobranca.com_quitacao_anotada(
        Cobranca.objects.filter(status=Cobranca.Status.ATRASADO)
    )
    semana_total = sum((c.valor for c in da_semana), ZERO)
    semana_recebido = sum((c.total_quitado for c in da_semana), ZERO)

    # prefetch das manutenções: auxilio_disponivel lê dias_parado de cada
    # sinistro, o que consultava uma vez por sinistro aberto
    sinistros_abertos = (
        Sinistro.objects.exclude(status=Sinistro.Status.CONCLUIDO)
        .select_related("veiculo")
        .prefetch_related("manutencoes")
    )
    fichas, _ = ranking_da_frota(hoje)
    contexto = {
        "hoje": hoje,
        "total_veiculos": contagem["total"],
        # (valor, rótulo, quantidade) — o valor vira o filtro do card no hub da frota
        "veiculos_por_status": [
            (valor, rotulo, contagem[f"status_{valor}"]) for valor, rotulo in status_no_painel
        ],
        "ocupacao": (
            round(100 * contagem["alugados"] / contagem["locacao"]) if contagem["locacao"] else None
        ),
        "trocas_em_andamento": TrocaTemporaria.objects.filter(
            data_devolucao__isnull=True
        ).select_related("alocacao__cliente", "veiculo_substituto"),
        "leituras_pendentes": veiculos_com_leitura_pendente(hoje),
        "alertas_preventivas": preventivas_em_alerta(),
        "semana_total": semana_total,
        "semana_recebido": semana_recebido,
        "percentual_semana": (
            min(100, round(100 * semana_recebido / semana_total)) if semana_total else 0
        ),
        "atrasado_total": sum((c.saldo for c in atrasadas), ZERO),
        "inadimplentes": Cliente.objects.filter(status=Cliente.Status.INADIMPLENTE),
        "nds_abertas": Cobranca.objects.filter(
            origem=Cobranca.Origem.NOTA_DEBITO, status__in=Cobranca.STATUS_EM_ABERTO
        ).count(),
        "fici_em_alerta": alertas_fici(hoje),
        "auxilios_disponiveis": [s for s in sinistros_abertos if s.auxilio_disponivel],
        "candidatos_venda": [f for f in fichas if f.nivel in ("preparar", "vender")],
        "vigencias": vigencias_a_vencer(hoje),
    }
    return render(request, "painel.html", contexto)
