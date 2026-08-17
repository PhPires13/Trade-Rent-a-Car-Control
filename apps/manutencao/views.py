from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from apps.frota.models import Veiculo

from .models import IntervaloPersonalizado, ItemPreventiva, Manutencao
from .repasse import gerar_repasse
from .services import StatusPreventiva, resumos_por_veiculo


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


class ItemPreventivaForm(forms.ModelForm):
    """Item do plano — com intervalo vira preventiva por km, sem intervalo é esporádica."""

    class Meta:
        model = ItemPreventiva
        fields = ["nome", "intervalo_km_padrao", "ativo"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 0 seria descartado no cálculo (falsy) e mentiria na tela — revisão etapa 8.
        self.fields["intervalo_km_padrao"].validators.append(MinValueValidator(1))
        self.fields["intervalo_km_padrao"].widget.attrs["min"] = 1

    def clean_intervalo_km_padrao(self):
        intervalo = self.cleaned_data.get("intervalo_km_padrao")
        if intervalo is None and self.instance.pk and self.instance.intervalos.exists():
            raise forms.ValidationError(
                "Este item tem intervalos personalizados por veículo — remova-os "
                "antes de torná-lo esporádico, senão eles seriam ignorados em silêncio."
            )
        return intervalo


class IntervaloPersonalizadoForm(forms.ModelForm):
    """Intervalo de km de um item para um veículo específico (docs.md §4.5)."""

    class Meta:
        model = IntervaloPersonalizado
        fields = ["veiculo", "item", "intervalo_km"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["veiculo"].queryset = Veiculo.objects.filter(uso=Veiculo.Uso.LOCACAO).exclude(
            status=Veiculo.Status.VENDIDO
        )
        self.fields["item"].queryset = ItemPreventiva.objects.filter(
            intervalo_km_padrao__isnull=False
        )
        self.fields["intervalo_km"].validators.append(MinValueValidator(1))
        self.fields["intervalo_km"].widget.attrs["min"] = 1


def _pk_do_post(request, chave):
    """IDs de POST adulterados viram 404, nao 500 (revisao etapa 8)."""
    valor = (request.POST.get(chave) or "").strip()
    if not valor.isdigit():
        raise Http404("Registro invalido.")
    return int(valor)


def _mensagem_de_erros(form):
    return "; ".join(erro for erros in form.errors.values() for erro in erros)


def plano(request):
    """Plano de manutenção editável: itens do plano e intervalos por veículo (docs.md §4.5)."""
    form_item = ItemPreventivaForm()
    form_intervalo = IntervaloPersonalizadoForm()
    if request.method == "POST":
        acao = request.POST.get("acao")
        if acao == "editar_item":
            item = get_object_or_404(ItemPreventiva, pk=_pk_do_post(request, "item_id"))
            form = ItemPreventivaForm(request.POST, instance=item)
            if form.is_valid():
                form.save()
                messages.success(request, f"Item {item.nome} atualizado.")
            else:
                messages.error(request, _mensagem_de_erros(form))
            return redirect("manutencao:plano")
        if acao == "novo_item":
            form_item = ItemPreventivaForm(request.POST)
            if form_item.is_valid():
                item = form_item.save()
                messages.success(request, f"Item {item.nome} incluído no plano.")
                return redirect("manutencao:plano")
        elif acao == "novo_intervalo":
            form_intervalo = IntervaloPersonalizadoForm(request.POST)
            if form_intervalo.is_valid():
                intervalo = form_intervalo.save()
                messages.success(request, f"Intervalo personalizado: {intervalo}.")
                return redirect("manutencao:plano")
        elif acao == "remover_intervalo":
            intervalo = get_object_or_404(
                IntervaloPersonalizado, pk=_pk_do_post(request, "intervalo_id")
            )
            intervalo.delete()
            messages.success(
                request,
                f"Intervalo personalizado removido — {intervalo.item.nome} volta ao padrão.",
            )
            return redirect("manutencao:plano")
    return render(
        request,
        "manutencao/plano.html",
        {
            "itens": ItemPreventiva.objects.all(),
            "personalizados": IntervaloPersonalizado.objects.select_related(
                "veiculo", "item"
            ).order_by("veiculo__placa", "item__nome"),
            "form_item": form_item,
            "form_intervalo": form_intervalo,
        },
    )


def preventivas(request):
    """Plano de preventivas da frota — status por veículo × item (docs.md §4.5)."""
    veiculos = (
        Veiculo.objects.filter(uso=Veiculo.Uso.LOCACAO)
        .exclude(status__in=[Veiculo.Status.VENDIDO, Veiculo.Status.INATIVO])
        .order_by("placa")
    )
    veiculos = list(veiculos)
    resumos = resumos_por_veiculo(veiculos)  # 3 queries fixas, não 6 por veículo
    quadro = [(veiculo, resumos[veiculo.pk]) for veiculo in veiculos]
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
