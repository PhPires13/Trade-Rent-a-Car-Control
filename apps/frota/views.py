from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render

from . import desmobilizacao
from .models import Veiculo


def ranking(request):
    """Ranking de desmobilização — candidatos à venda primeiro (docs.md §4.9)."""
    fichas, media = desmobilizacao.ranking_da_frota()
    vendidos = Veiculo.objects.filter(status=Veiculo.Status.VENDIDO).order_by("-data_venda")
    fichas_vendidos = [desmobilizacao.montar_ficha(v) for v in vendidos]
    return render(
        request,
        "frota/ranking.html",
        {"fichas": fichas, "media": media, "fichas_vendidos": fichas_vendidos},
    )


def ficha(request, veiculo_id):
    veiculo = get_object_or_404(Veiculo, pk=veiculo_id)
    _, media = desmobilizacao.ranking_da_frota()
    ficha = desmobilizacao.avaliar(desmobilizacao.montar_ficha(veiculo), media)
    return render(request, "frota/ficha.html", {"f": ficha, "veiculo": veiculo})


def vender(request, veiculo_id):
    veiculo = get_object_or_404(Veiculo, pk=veiculo_id)
    if request.method == "POST":
        try:
            desmobilizacao.registrar_venda(
                veiculo,
                data=date.fromisoformat(request.POST["data"]),
                valor=Decimal(request.POST["valor"].replace(",", ".")),
                comprador=request.POST.get("comprador", ""),
                custos=(
                    Decimal(request.POST["custos"].replace(",", "."))
                    if request.POST.get("custos", "").strip()
                    else None
                ),
                km=int(request.POST["km"]) if request.POST.get("km", "").strip() else None,
            )
            messages.success(
                request,
                f"{veiculo.placa} vendido — crédito da venda fica fora da base do DAS. "
                "Resultado final na ficha do veículo.",
            )
            return redirect("frota:ficha", veiculo.pk)
        except (ValidationError, KeyError, InvalidOperation, ValueError) as erro:
            mensagens = getattr(erro, "messages", ["Preencha os campos corretamente."])
            messages.error(request, "; ".join(mensagens))
    return render(request, "frota/vender.html", {"veiculo": veiculo, "hoje": date.today()})
