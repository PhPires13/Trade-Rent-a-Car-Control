from datetime import date

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render

from apps.frota.models import Veiculo

from .models import RegistroKm, primeiro_dia_do_mes, veiculos_com_leitura_pendente


def _mes_da_query(request):
    valor = request.GET.get("mes")
    if valor:
        try:
            ano, mes = valor.split("-")
            return date(int(ano), int(mes), 1)
        except (ValueError, TypeError):
            pass
    return primeiro_dia_do_mes(date.today())


def lista_mensal(request):
    """Registro mensal de KM — pendências e leituras do mês (docs.md §4.8)."""
    from apps.alocacoes.services import cliente_vigente

    mes = _mes_da_query(request)
    registros = list(
        RegistroKm.objects.filter(mes_referencia=mes)
        .select_related("veiculo")
        .order_by("veiculo__placa")
    )
    for registro in registros:
        registro.cliente = cliente_vigente(registro.veiculo, registro.data_leitura)
    pendentes = veiculos_com_leitura_pendente(mes)
    return render(
        request,
        "km/lista.html",
        {
            "mes": mes,
            "registros": registros,
            "pendentes": pendentes,
            "hoje": date.today(),
        },
    )


def registrar(request, veiculo_id):
    veiculo = get_object_or_404(Veiculo, pk=veiculo_id)
    if request.method != "POST":
        return redirect("km:lista")
    try:
        data_leitura = date.fromisoformat(request.POST["data_leitura"])
        registro = RegistroKm(
            veiculo=veiculo,
            mes_referencia=primeiro_dia_do_mes(data_leitura),
            data_leitura=data_leitura,
            km=int(request.POST["km"]),
            observacoes=request.POST.get("observacoes", ""),
        )
        registro.full_clean()
        registro.save()
        messages.success(request, f"KM de {veiculo.placa} registrado: {registro.km} km.")
    except ValidationError as erro:
        messages.error(request, f"{veiculo.placa}: {'; '.join(erro.messages)}")
    except (KeyError, ValueError):
        messages.error(request, f"{veiculo.placa}: preencha a data e o KM corretamente.")
    return redirect("km:lista")


def historico(request, veiculo_id):
    veiculo = get_object_or_404(Veiculo, pk=veiculo_id)
    registros = veiculo.registros_km.order_by("-mes_referencia")
    return render(request, "km/historico.html", {"veiculo": veiculo, "registros": registros})
