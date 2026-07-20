from datetime import date

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render

from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente

from .models import Alocacao, TrocaTemporaria
from .services import linha_do_tempo


class AlocacaoForm(forms.ModelForm):
    class Meta:
        model = Alocacao
        fields = [
            "veiculo",
            "cliente",
            "data_inicio",
            "valor_semanal",
            "dia_vencimento",
            "caucao_valor",
            "km_entrega",
            "limite_km",
            "franquia_km_mensal",
            "taxa_km_excedido",
            "observacoes",
        ]
        widgets = {
            "data_inicio": forms.DateInput(attrs={"type": "date"}),
            "observacoes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["veiculo"].queryset = Veiculo.objects.filter(
            status=Veiculo.Status.DISPONIVEL, uso=Veiculo.Uso.LOCACAO
        )
        self.fields["cliente"].queryset = Cliente.objects.exclude(status=Cliente.Status.INATIVO)
        self.fields["dia_vencimento"].required = False

    def clean_dia_vencimento(self):
        valor = self.cleaned_data.get("dia_vencimento")
        return None if valor in (None, "") else valor


class TrocaForm(forms.ModelForm):
    class Meta:
        model = TrocaTemporaria
        fields = [
            "veiculo_substituto",
            "data_retirada",
            "km_retirada",
            "motivo",
            "valor_semanal_ajustado",
            "observacoes",
        ]
        widgets = {
            "data_retirada": forms.DateInput(attrs={"type": "date"}),
            "observacoes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["veiculo_substituto"].queryset = Veiculo.objects.filter(
            status=Veiculo.Status.DISPONIVEL, uso=Veiculo.Uso.LOCACAO
        )


def lista(request):
    mostrar = request.GET.get("mostrar", "ativas")
    alocacoes = Alocacao.objects.select_related("veiculo", "cliente").prefetch_related("trocas")
    if mostrar == "ativas":
        alocacoes = alocacoes.filter(status=Alocacao.Status.ATIVA)
    return render(request, "alocacoes/lista.html", {"alocacoes": alocacoes, "mostrar": mostrar})


def nova(request):
    form = AlocacaoForm(request.POST or None, initial={"data_inicio": date.today()})
    if request.method == "POST" and form.is_valid():
        alocacao = form.save(commit=False)
        if alocacao.dia_vencimento is None:
            alocacao.dia_vencimento = alocacao.data_inicio.weekday()
        alocacao.save()
        cliente = alocacao.cliente
        if cliente.cnh_validade and cliente.cnh_validade < date.today():
            messages.warning(
                request,
                f"Atenção: a CNH de {cliente.nome} está vencida "
                f"desde {cliente.cnh_validade:%d/%m/%Y}.",
            )
        messages.success(request, f"Veículo {alocacao.veiculo.placa} alocado a {cliente.nome}.")
        return redirect("alocacoes:lista")
    return render(request, "alocacoes/nova.html", {"form": form})


def encerrar(request, alocacao_id):
    alocacao = get_object_or_404(Alocacao, pk=alocacao_id)
    if request.method == "POST":
        try:
            alocacao.encerrar(
                data_termino=date.fromisoformat(request.POST["data_termino"]),
                km_devolucao=int(request.POST["km_devolucao"]),
            )
            messages.success(
                request,
                f"Alocação encerrada — {alocacao.veiculo.placa} está disponível. "
                "O acerto de caução entra na etapa financeira.",
            )
            return redirect("alocacoes:lista")
        except (ValidationError, KeyError, ValueError) as erro:
            mensagens = getattr(erro, "messages", ["Preencha a data e o KM corretamente."])
            messages.error(request, "; ".join(mensagens))
    return render(request, "alocacoes/encerrar.html", {"alocacao": alocacao, "hoje": date.today()})


def troca_nova(request, alocacao_id):
    alocacao = get_object_or_404(Alocacao, pk=alocacao_id)
    form = TrocaForm(request.POST or None, initial={"data_retirada": date.today()})
    if request.method == "POST" and form.is_valid():
        troca = form.save(commit=False)
        troca.alocacao = alocacao
        try:
            troca.full_clean()
            troca.save()
            messages.success(
                request,
                f"{alocacao.cliente.nome} está com o substituto {troca.veiculo_substituto.placa}.",
            )
            return redirect("alocacoes:lista")
        except ValidationError as erro:
            for mensagens in erro.message_dict.values():
                for mensagem in mensagens:
                    form.add_error(None, mensagem)
    return render(request, "alocacoes/troca_nova.html", {"alocacao": alocacao, "form": form})


def troca_devolver(request, troca_id):
    troca = get_object_or_404(TrocaTemporaria, pk=troca_id)
    if request.method == "POST":
        try:
            troca.devolver(
                data_devolucao=date.fromisoformat(request.POST["data_devolucao"]),
                km_devolucao=int(request.POST["km_devolucao"]),
            )
            messages.success(request, f"Substituto {troca.veiculo_substituto.placa} devolvido.")
            return redirect("alocacoes:lista")
        except (ValidationError, KeyError, ValueError) as erro:
            mensagens = getattr(erro, "messages", ["Preencha a data e o KM corretamente."])
            messages.error(request, "; ".join(mensagens))
    return render(request, "alocacoes/troca_devolver.html", {"troca": troca, "hoje": date.today()})


def timeline(request, veiculo_id):
    veiculo = get_object_or_404(Veiculo, pk=veiculo_id)
    return render(
        request,
        "alocacoes/linha_tempo.html",
        {"veiculo": veiculo, "eventos": linha_do_tempo(veiculo)},
    )
