import csv
from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from apps.pessoas.models import Cliente

from . import services
from .models import ZERO, Caucao, Cobranca, MovimentacaoCaucao, MovimentoCredito, NotaDebito

ABERTAS = [Cobranca.Status.PENDENTE, Cobranca.Status.PARCIAL, Cobranca.Status.ATRASADO]


def cobrancas(request):
    """Painel de cobranças com filtros e sugestão de encargo (docs.md §4.3)."""
    qs = Cobranca.objects.select_related("cliente", "alocacao__veiculo").order_by("vencimento")
    status = request.GET.get("status", "abertas")
    origem = request.GET.get("origem", "")
    if status == "abertas":
        qs = qs.filter(status__in=ABERTAS)
    elif status != "todas":
        qs = qs.filter(status=status)
    if origem:
        qs = qs.filter(origem=origem)
    hoje = date.today()
    linhas = []
    for cobranca in qs[:300]:
        encargo = (
            services.sugerir_encargo(cobranca, hoje)
            if cobranca.status == Cobranca.Status.ATRASADO
            and cobranca.origem != Cobranca.Origem.ENCARGO
            and not cobranca.encargos.exists()
            else None
        )
        linhas.append((cobranca, encargo))
    return render(
        request,
        "financeiro/cobrancas.html",
        {
            "linhas": linhas,
            "status": status,
            "origem": origem,
            "origens": Cobranca.Origem.choices,
            "statuses": Cobranca.Status.choices,
        },
    )


def aplicar_encargo(request, cobranca_id):
    cobranca = get_object_or_404(Cobranca, pk=cobranca_id)
    if request.method == "POST":
        try:
            valor = Decimal(request.POST["valor"].replace(",", "."))
            services.aplicar_encargo(cobranca, valor)
            messages.success(request, f"Encargo de R$ {valor} aplicado.")
        except (ValidationError, KeyError, InvalidOperation) as erro:
            messages.error(request, "; ".join(getattr(erro, "messages", ["Valor inválido."])))
    return redirect("financeiro:cobrancas")


def marcar_judicial(request, cobranca_id):
    cobranca = get_object_or_404(Cobranca, pk=cobranca_id)
    if request.method == "POST":
        cobranca.status = Cobranca.Status.JUDICIAL
        cobranca.save(update_fields=["status"])
        messages.success(request, f"'{cobranca.descricao}' marcada como cobrança judicial.")
    return redirect("financeiro:cobrancas")


def receber(request):
    """Baixa de recebimento com travas de lançamento (docs.md §4.3)."""
    clientes = Cliente.objects.order_by("nome")
    cliente = None
    cobrancas_abertas = []
    cliente_id = request.GET.get("cliente") or request.POST.get("cliente")
    if cliente_id:
        cliente = get_object_or_404(Cliente, pk=cliente_id)
        cobrancas_abertas = list(
            cliente.cobrancas.filter(status__in=ABERTAS).order_by("vencimento")
        )
    if request.method == "POST" and cliente:
        try:
            valor = Decimal(request.POST["valor"].replace(",", "."))
            data = date.fromisoformat(request.POST["data"])
            forma = request.POST["forma"]
            aplicacoes = []
            for cobranca in cobrancas_abertas:
                bruto = request.POST.get(f"aplicar_{cobranca.pk}", "").strip()
                if bruto:
                    aplicacoes.append((cobranca, Decimal(bruto.replace(",", "."))))
            services.registrar_recebimento(
                cliente=cliente,
                data=data,
                valor=valor,
                forma=forma,
                aplicacoes=aplicacoes,
                sobra_destino=request.POST.get("sobra_destino", "credito"),
                observacoes=request.POST.get("observacoes", ""),
            )
            messages.success(request, f"Recebimento de R$ {valor} lançado para {cliente.nome}.")
            return redirect(f"{request.path}?cliente={cliente.pk}")
        except ValidationError as erro:
            messages.error(request, "; ".join(erro.messages))
        except (KeyError, InvalidOperation, ValueError):
            messages.error(request, "Preencha valor, data e aplicações corretamente.")
    credito = MovimentoCredito.saldo_do_cliente(cliente) if cliente else ZERO
    return render(
        request,
        "financeiro/receber.html",
        {
            "clientes": clientes,
            "cliente": cliente,
            "cobrancas_abertas": cobrancas_abertas,
            "credito": credito,
            "hoje": date.today(),
            "formas": [
                ("pix", "Pix"),
                ("dinheiro", "Dinheiro"),
                ("transferencia", "Transferência"),
                ("credito", "Crédito do cliente"),
            ],
        },
    )


def nds(request):
    notas = NotaDebito.objects.select_related("cliente").prefetch_related("itens", "cobranca")
    return render(request, "financeiro/nds.html", {"notas": notas})


def nd_nova(request):
    clientes = Cliente.objects.order_by("nome")
    if request.method == "POST":
        try:
            cliente = get_object_or_404(Cliente, pk=request.POST["cliente"])
            descricoes = request.POST.getlist("item_descricao")
            valores = request.POST.getlist("item_valor")
            itens = [
                (descricao.strip(), Decimal(valor.replace(",", ".")))
                for descricao, valor in zip(descricoes, valores, strict=False)
                if descricao.strip() and valor.strip()
            ]
            nd = services.emitir_nota_debito(
                cliente=cliente,
                data_emissao=date.fromisoformat(request.POST["data_emissao"]),
                vencimento=(
                    date.fromisoformat(request.POST["vencimento"])
                    if request.POST.get("vencimento")
                    else None
                ),
                itens=itens,
                observacoes=request.POST.get("observacoes", ""),
            )
            messages.success(request, f"ND {nd.numero:03d} emitida (R$ {nd.total}).")
            return redirect("financeiro:nds")
        except ValidationError as erro:
            messages.error(request, "; ".join(erro.messages))
        except (KeyError, InvalidOperation, ValueError):
            messages.error(request, "Preencha cliente, data e itens corretamente.")
    return render(request, "financeiro/nd_nova.html", {"clientes": clientes, "hoje": date.today()})


def caucoes(request):
    lista = Caucao.objects.select_related("alocacao__cliente", "alocacao__veiculo")
    return render(request, "financeiro/caucoes.html", {"caucoes": lista})


def caucao_detalhe(request, caucao_id):
    caucao = get_object_or_404(Caucao, pk=caucao_id)
    cliente = caucao.alocacao.cliente
    pendentes = cliente.cobrancas.filter(status__in=ABERTAS).order_by("vencimento")
    if request.method == "POST":
        try:
            tipo = request.POST["tipo"]
            valor = Decimal(request.POST["valor"].replace(",", "."))
            data = date.fromisoformat(request.POST["data"])
            if tipo == "desconto":
                cobranca = get_object_or_404(Cobranca, pk=request.POST["cobranca"], cliente=cliente)
                services.descontar_da_caucao(
                    caucao,
                    cobranca,
                    valor,
                    data,
                    observacoes=request.POST.get("observacoes", ""),
                )
            else:
                if tipo == "devolucao" and valor > caucao.saldo:
                    raise ValidationError(f"Devolução acima do saldo (R$ {caucao.saldo}).")
                MovimentacaoCaucao.objects.create(
                    caucao=caucao,
                    tipo=tipo,
                    valor=valor,
                    data=data,
                    forma=request.POST.get("forma", ""),
                    observacoes=request.POST.get("observacoes", ""),
                )
            messages.success(request, "Movimentação registrada.")
            return redirect("financeiro:caucao_detalhe", caucao.pk)
        except ValidationError as erro:
            messages.error(request, "; ".join(erro.messages))
        except (KeyError, InvalidOperation, ValueError):
            messages.error(request, "Preencha os campos corretamente.")
    return render(
        request,
        "financeiro/caucao_detalhe.html",
        {"caucao": caucao, "pendentes": pendentes, "hoje": date.today()},
    )


def _ano_mes(request):
    try:
        ano, mes = request.GET.get("mes", "").split("-")
        return int(ano), int(mes)
    except (ValueError, AttributeError):
        hoje = date.today()
        return hoje.year, hoje.month


def das(request):
    """Base de cálculo do DAS — só receita de locação é tributável (decisão nº 11)."""
    ano, mes = _ano_mes(request)
    resumo = services.resumo_fiscal(ano, mes)
    if request.GET.get("exportar") == "csv":
        resposta = HttpResponse(content_type="text/csv")
        resposta["Content-Disposition"] = f'attachment; filename="das-{ano}-{mes:02d}.csv"'
        writer = csv.writer(resposta)
        writer.writerow(["Grupo", "Valor (R$)", "Entra na base do DAS?"])
        writer.writerow(["Receita de locação (fatura)", resumo["locacao"], "Sim"])
        for rotulo, valor in resumo["diversos"].items():
            writer.writerow([f"Pagamentos diversos — {rotulo} (ND)", valor, "Não"])
        writer.writerow(["Caução recebida", resumo["caucao_recebida"], "Não"])
        return resposta
    return render(
        request,
        "financeiro/das.html",
        {"resumo": resumo, "ano": ano, "mes": mes, "mes_str": f"{ano}-{mes:02d}"},
    )


def extrato_cliente(request, cliente_id):
    cliente = get_object_or_404(Cliente, pk=cliente_id)
    return render(
        request,
        "financeiro/extrato_cliente.html",
        {
            "cliente": cliente,
            "cobrancas": cliente.cobrancas.order_by("-vencimento")[:100],
            "recebimentos": cliente.recebimentos.prefetch_related("aplicacoes__cobranca")[:50],
            "credito": MovimentoCredito.saldo_do_cliente(cliente),
        },
    )
