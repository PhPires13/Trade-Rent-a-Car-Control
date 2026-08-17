from datetime import date
from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import urlencode

from .models import AuxilioMotorista, Sinistro


class SinistroForm(forms.ModelForm):
    class Meta:
        model = Sinistro
        fields = [
            "veiculo",
            "data",
            "cliente",
            "tipo",
            "envolvido",
            "responsabilidade",
            "descricao",
            "boletim_ocorrencia",
            "acionou_protecao",
            "data_evento",
            "franquia_valor",
            "responsavel_custo",
            "status",
            "observacoes",
        ]
        widgets = {
            "data": forms.DateInput(attrs={"type": "date"}),
            "data_evento": forms.DateInput(attrs={"type": "date"}),
            "descricao": forms.Textarea(attrs={"rows": 3}),
            "observacoes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["cliente"].required = False
        self.fields[
            "cliente"
        ].help_text = "Deixe vazio para preencher com quem estava com o carro na data"


def lista(request):
    sinistros = Sinistro.objects.select_related("veiculo", "cliente").prefetch_related(
        "manutencoes", "auxilios"
    )
    status = request.GET.get("status")
    if status:
        sinistros = sinistros.filter(status=status)
    pagina = Paginator(sinistros, 25).get_page(request.GET.get("pagina"))
    return render(
        request,
        "sinistros/lista.html",
        {
            "sinistros": pagina.object_list,
            "pagina": pagina,
            "filtros": urlencode({"status": status or ""}),
            "statuses": Sinistro.Status.choices,
            "filtro": status or "",
        },
    )


def novo(request):
    form = SinistroForm(request.POST or None, initial={"data": date.today()})
    if request.method == "POST" and form.is_valid():
        sinistro = form.save()
        nome = sinistro.cliente.nome if sinistro.cliente else "nenhum cliente na data"
        messages.success(request, f"Sinistro registrado — motorista: {nome}.")
        return redirect("sinistros:lista")
    return render(request, "sinistros/novo.html", {"form": form})


def solicitar_auxilio(request, sinistro_id):
    sinistro = get_object_or_404(Sinistro, pk=sinistro_id)
    if request.method == "POST":
        try:
            valor = request.POST.get("valor", "").strip()
            AuxilioMotorista.objects.create(
                sinistro=sinistro,
                valor=Decimal(valor.replace(",", ".")) if valor else None,
                status=AuxilioMotorista.Status.SOLICITADO,
                data_solicitacao=date.today(),
            )
            messages.success(request, "Auxílio motorista registrado como solicitado.")
        except InvalidOperation:
            messages.error(request, "Valor inválido.")
    return redirect("sinistros:lista")


def receber_auxilio(request, auxilio_id):
    auxilio = get_object_or_404(AuxilioMotorista, pk=auxilio_id)
    if request.method == "POST":
        try:
            valor = request.POST.get("valor", "").strip()
            if valor:
                auxilio.valor = Decimal(valor.replace(",", "."))
            auxilio.status = AuxilioMotorista.Status.RECEBIDO
            auxilio.data_recebimento = date.today()
            auxilio.save()
            messages.success(
                request,
                f"Auxílio de R$ {auxilio.valor} recebido — outro crédito, fora da base do DAS.",
            )
        except InvalidOperation:
            messages.error(request, "Valor inválido.")
    return redirect("sinistros:lista")
