from datetime import date

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from apps.pessoas.models import Cliente

from .models import Multa
from .services import emitir_nota_debito, preencher_cliente_alocacao


class MultaForm(forms.ModelForm):
    class Meta:
        model = Multa
        fields = [
            "veiculo",
            "data",
            "codigo",
            "ait",
            "num_processamento",
            "orgao",
            "descricao",
            "local",
            "valor",
            "pontos",
            "resultado",
            "tipo_condutor",
            "condutor_autorizado",
            "fici_status",
            "fici_prazo",
            "pagamento",
            "pago_por",
            "repasse",
            "responsavel",
            "observacoes",
        ]
        widgets = {
            "data": forms.DateInput(attrs={"type": "date"}),
            "fici_prazo": forms.DateInput(attrs={"type": "date"}),
            "descricao": forms.Textarea(attrs={"rows": 2}),
            "observacoes": forms.Textarea(attrs={"rows": 2}),
        }


def lista(request):
    filtro = request.GET.get("filtro", "")
    multas = Multa.objects.select_related("veiculo", "cliente_alocacao", "orgao").order_by("-data")
    if filtro == "fici":
        multas = multas.filter(fici_status=Multa.FICI.PENDENTE)
    elif filtro == "a_cobrar":
        multas = multas.filter(repasse=Multa.Repasse.A_COBRAR)
    return render(request, "multas/lista.html", {"multas": multas, "filtro": filtro})


def nova(request):
    form = MultaForm(request.POST or None, initial={"data": date.today()})
    if request.method == "POST" and form.is_valid():
        multa = form.save(commit=False)
        preencher_cliente_alocacao(multa)
        multa.save()
        if multa.cliente_alocacao:
            messages.success(
                request,
                f"Multa registrada e vinculada a {multa.cliente_alocacao.nome} "
                "(cliente vigente na data).",
            )
        else:
            messages.warning(
                request,
                "Multa registrada, mas nenhum cliente estava com o carro nesta data — "
                "verifique o responsável.",
            )
        return redirect("multas:lista")
    return render(request, "multas/nova.html", {"form": form})


def emitir_nd(request):
    """Emite uma ND agrupando as multas 'a cobrar' de um cliente (docs.md §4.7)."""
    cliente_id = request.POST.get("cliente") or request.GET.get("cliente")
    cliente = get_object_or_404(Cliente, pk=cliente_id) if cliente_id else None
    multas = []
    if cliente:
        multas = list(
            Multa.objects.filter(
                cliente_alocacao=cliente, repasse=Multa.Repasse.A_COBRAR, valor__isnull=False
            ).select_related("veiculo")
        )
    if request.method == "POST" and cliente and "confirmar" in request.POST:
        ids = request.POST.getlist("multa")
        selecionadas = [m for m in multas if str(m.id) in ids]
        nota = emitir_nota_debito(cliente, selecionadas)
        if nota:
            messages.success(
                request,
                f"ND {nota.numero:03d} emitida com {len(selecionadas)} multa(s) — "
                f"cobrança de R$ {nota.total:.2f} gerada.",
            )
            return redirect("multas:lista")
        messages.error(request, "Selecione ao menos uma multa a cobrar.")
    clientes = Cliente.objects.exclude(status=Cliente.Status.INATIVO).order_by("nome")
    return render(
        request,
        "multas/emitir_nd.html",
        {"clientes": clientes, "cliente": cliente, "multas": multas},
    )
