from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import urlencode
from django.views.decorators.cache import never_cache

from apps.pessoas.models import Cliente

from . import services
from .forms import MultaForm, OrgaoForm
from .models import Multa, OrgaoAutuador


def lista(request):
    multas = Multa.objects.select_related("veiculo", "cliente", "orgao")
    veiculo = request.GET.get("veiculo")
    if veiculo:
        multas = multas.filter(veiculo__placa__icontains=veiculo)
    resultado = request.GET.get("resultado")
    if resultado:
        multas = multas.filter(resultado=resultado)
    pagina = Paginator(multas, 50).get_page(request.GET.get("pagina"))
    return render(
        request,
        "multas/lista.html",
        {
            "multas": pagina.object_list,
            "pagina": pagina,
            "filtros": urlencode({"veiculo": veiculo or "", "resultado": resultado or ""}),
            "resultados": Multa.Resultado.choices,
            "filtro_veiculo": veiculo or "",
            "filtro_resultado": resultado or "",
            "hoje": date.today(),
        },
    )


def nova(request):
    form = MultaForm(request.POST or None, initial={"data_infracao": date.today()})
    if request.method == "POST" and form.is_valid():
        multa = form.save()
        nome = multa.cliente.nome if multa.cliente else "nenhum cliente na data"
        messages.success(request, f"Multa registrada — atribuída a: {nome}.")
        return redirect("multas:lista")
    return render(request, "multas/nova.html", {"form": form})


def indicar_fici(request, multa_id):
    multa = get_object_or_404(Multa, pk=multa_id)
    if request.method == "POST":
        multa.fici_status = Multa.Fici.INDICADO
        multa.fici_data_indicacao = date.today()
        multa.save(update_fields=["fici_status", "fici_data_indicacao"])
        messages.success(request, f"FICI indicado para {multa}.")
    return redirect("multas:lista")


def registrar_nic(request, multa_id):
    multa = get_object_or_404(Multa, pk=multa_id)
    if request.method == "POST":
        try:
            valor = request.POST.get("valor", "").strip()
            nic = services.registrar_nic(
                multa, valor=Decimal(valor.replace(",", ".")) if valor else None
            )
            messages.warning(request, f"NIC registrada contra a empresa: {nic}.")
        except (ValidationError, InvalidOperation) as erro:
            messages.error(request, "; ".join(getattr(erro, "messages", ["Valor inválido."])))
    return redirect("multas:lista")


def marcar_paga(request, multa_id):
    multa = get_object_or_404(Multa, pk=multa_id)
    if request.method == "POST":
        multa.pagamento = Multa.Pagamento.PAGO
        multa.pago_por = request.POST.get("pago_por", "")
        multa.save(update_fields=["pagamento", "pago_por"])
        messages.success(request, "Multa marcada como paga.")
    return redirect("multas:lista")


def gerar_nd(request):
    """Seleciona multas 'a cobrar' de um cliente e emite a ND (docs.md §4.7)."""
    clientes = Cliente.objects.order_by("nome")
    cliente = None
    a_cobrar = []
    cliente_id = request.GET.get("cliente") or request.POST.get("cliente")
    if cliente_id:
        cliente = get_object_or_404(Cliente, pk=cliente_id)
        a_cobrar = [m for m in cliente.multas.select_related("veiculo") if m.repasse == "A cobrar"]
    if request.method == "POST" and cliente:
        selecionadas = [m for m in a_cobrar if str(m.pk) in request.POST.getlist("multa")]
        try:
            nd = services.gerar_nd_de_multas(cliente, selecionadas, date.today())
            messages.success(request, f"ND {nd.numero:03d} emitida (R$ {nd.total}).")
            return redirect("financeiro:nds")
        except ValidationError as erro:
            messages.error(request, "; ".join(erro.messages))
    return render(
        request,
        "multas/gerar_nd.html",
        {"clientes": clientes, "cliente": cliente, "multas": a_cobrar},
    )


@never_cache
def orgaos(request):
    """Consulta de órgãos — sem cache: a tela pode revelar credenciais."""
    return render(request, "multas/orgaos.html", {"orgaos": OrgaoAutuador.objects.all()})


@never_cache
def orgao_novo(request):
    form = OrgaoForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        orgao = form.save()
        messages.success(request, f"Órgão {orgao.nome} cadastrado.")
        return redirect("multas:orgaos")
    return render(request, "multas/orgao_form.html", {"form": form, "orgao": None})


@never_cache
def orgao_editar(request, orgao_id):
    orgao = get_object_or_404(OrgaoAutuador, pk=orgao_id)
    form = OrgaoForm(request.POST or None, instance=orgao)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Órgão {orgao.nome} atualizado.")
        return redirect("multas:orgaos")
    return render(request, "multas/orgao_form.html", {"form": form, "orgao": orgao})
