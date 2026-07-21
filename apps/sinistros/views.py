from datetime import date

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from .models import AuxilioMotorista, Sinistro
from .services import preencher_motorista, registrar_auxilio, sinistros_com_auxilio_a_solicitar


class SinistroForm(forms.ModelForm):
    class Meta:
        model = Sinistro
        fields = [
            "veiculo",
            "data",
            "tipo",
            "envolvido",
            "responsabilidade",
            "descricao",
            "boletim_ocorrencia",
            "acionou_protecao",
            "data_evento",
            "franquia_valor",
            "situacao_veiculo",
            "status",
            "observacoes",
        ]
        widgets = {
            "data": forms.DateInput(attrs={"type": "date"}),
            "data_evento": forms.DateInput(attrs={"type": "date"}),
            "descricao": forms.Textarea(attrs={"rows": 2}),
            "observacoes": forms.Textarea(attrs={"rows": 2}),
        }


def lista(request):
    sinistros = Sinistro.objects.select_related("veiculo", "motorista").order_by("-data")
    auxilios = sinistros_com_auxilio_a_solicitar()
    return render(request, "sinistros/lista.html", {"sinistros": sinistros, "auxilios": auxilios})


def novo(request):
    form = SinistroForm(request.POST or None, initial={"data": date.today()})
    if request.method == "POST" and form.is_valid():
        sinistro = form.save(commit=False)
        preencher_motorista(sinistro)
        sinistro.save()
        messages.success(
            request,
            f"Sinistro registrado para {sinistro.veiculo.placa}"
            + (f" (motorista: {sinistro.motorista.nome})." if sinistro.motorista else "."),
        )
        return redirect("sinistros:lista")
    return render(request, "sinistros/novo.html", {"form": form})


def solicitar_auxilio(request, sinistro_id):
    sinistro = get_object_or_404(Sinistro, pk=sinistro_id)
    if request.method == "POST":
        dias = int(request.POST.get("dias", 0))
        valor = request.POST.get("valor") or None
        registrar_auxilio(sinistro, dias, valor)
        messages.success(
            request,
            f"Auxílio motorista registrado para {sinistro.veiculo.placa} (a solicitar).",
        )
    return redirect("sinistros:lista")


def auxilios(request):
    registros = AuxilioMotorista.objects.select_related("sinistro__veiculo").order_by("-id")
    return render(request, "sinistros/auxilios.html", {"auxilios": registros})
