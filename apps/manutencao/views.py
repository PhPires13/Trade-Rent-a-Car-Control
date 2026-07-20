from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from apps.frota.models import Veiculo

from .models import ItemPreventiva, Manutencao
from .services import StatusPreventiva, resumo_preventivas


class ManutencaoForm(forms.ModelForm):
    class Meta:
        model = Manutencao
        fields = ["item", "tipo", "data", "km", "descricao", "observacoes"]
        widgets = {
            "data": forms.DateInput(attrs={"type": "date"}),
            "descricao": forms.Textarea(attrs={"rows": 3}),
            "observacoes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["item"].queryset = ItemPreventiva.objects.filter(ativo=True)


def preventivas(request):
    """Plano de preventivas da frota — status por veículo × item (docs.md §4.5)."""
    veiculos = (
        Veiculo.objects.filter(uso=Veiculo.Uso.LOCACAO)
        .exclude(status__in=[Veiculo.Status.VENDIDO, Veiculo.Status.INATIVO])
        .order_by("placa")
    )
    quadro = [(veiculo, resumo_preventivas(veiculo)) for veiculo in veiculos]
    return render(
        request,
        "manutencao/preventivas.html",
        {"quadro": quadro, "Status": StatusPreventiva},
    )


def registrar(request, veiculo_id):
    veiculo = get_object_or_404(Veiculo, pk=veiculo_id)
    form = ManutencaoForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        manutencao = form.save(commit=False)
        manutencao.veiculo = veiculo
        manutencao.save()
        messages.success(request, f"Manutenção registrada para {veiculo.placa}.")
        return redirect("manutencao:preventivas")
    return render(request, "manutencao/registrar.html", {"veiculo": veiculo, "form": form})


def historico(request, veiculo_id):
    veiculo = get_object_or_404(Veiculo, pk=veiculo_id)
    manutencoes = veiculo.manutencoes.select_related("item").order_by("-data")
    return render(
        request,
        "manutencao/historico.html",
        {"veiculo": veiculo, "manutencoes": manutencoes},
    )
