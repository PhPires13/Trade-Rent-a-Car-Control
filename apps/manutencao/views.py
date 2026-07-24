from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render

from apps.frota.models import Veiculo

from .models import ItemPreventiva, Manutencao
from .repasse import gerar_repasse
from .services import StatusPreventiva, resumo_preventivas


class ManutencaoForm(forms.ModelForm):
    class Meta:
        model = Manutencao
        fields = [
            "item",
            "tipo",
            "data",
            "km",
            "descricao",
            "oficina",
            "data_entrada",
            "data_saida",
            "origem_custo",
            "custo_real",
            "valor_cobrado_cliente",
            "responsavel",
            "pagamento_custo",
            "sinistro",
            "observacoes",
        ]
        widgets = {
            "data": forms.DateInput(attrs={"type": "date"}),
            "data_entrada": forms.DateInput(attrs={"type": "date"}),
            "data_saida": forms.DateInput(attrs={"type": "date"}),
            "descricao": forms.Textarea(attrs={"rows": 3}),
            "observacoes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, veiculo=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["item"].queryset = ItemPreventiva.objects.filter(ativo=True)
        if veiculo is not None:
            self.fields["sinistro"].queryset = veiculo.sinistros.all()


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
    form = ManutencaoForm(request.POST or None, veiculo=veiculo)
    if request.method == "POST" and form.is_valid():
        manutencao = form.save(commit=False)
        manutencao.veiculo = veiculo
        manutencao.save()
        messages.success(request, f"Manutenção registrada para {veiculo.placa}.")
        if (
            manutencao.responsavel == Manutencao.Responsavel.CLIENTE
            and manutencao.valor_cobrado_cliente
        ):
            messages.info(
                request,
                "Custo do cliente — gere a cobrança de repasse no histórico do veículo.",
            )
        return redirect("manutencao:preventivas")
    return render(request, "manutencao/registrar.html", {"veiculo": veiculo, "form": form})


def repassar(request, manutencao_id):
    manutencao = get_object_or_404(Manutencao, pk=manutencao_id)
    if request.method == "POST":
        try:
            cobranca = gerar_repasse(manutencao)
            messages.success(
                request, f"Repasse de R$ {cobranca.valor} cobrado de {cobranca.cliente.nome}."
            )
        except ValidationError as erro:
            messages.error(request, "; ".join(erro.messages))
    return redirect("manutencao:historico", manutencao.veiculo_id)


def historico(request, veiculo_id):
    veiculo = get_object_or_404(Veiculo, pk=veiculo_id)
    manutencoes = veiculo.manutencoes.select_related("item", "oficina", "sinistro").order_by(
        "-data"
    )
    return render(
        request,
        "manutencao/historico.html",
        {"veiculo": veiculo, "manutencoes": manutencoes},
    )
