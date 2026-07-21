import csv
from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from apps.pessoas.models import Cliente

from . import reports
from .models import Caucao, Cobranca, NotaDebito, Recebimento
from .services import (
    aplicar_encargo,
    cobrancas_em_aberto,
    registrar_recebimento,
    sugerir_encargo,
)


def painel_cobrancas(request):
    """Todas as cobranças em aberto por cliente (docs.md §4.3)."""
    filtro_status = request.GET.get("status", "")
    cobrancas = (
        Cobranca.objects.exclude(status=Cobranca.Status.PAGO)
        .select_related("cliente", "alocacao__veiculo")
        .order_by("cliente__nome", "vencimento")
    )
    if filtro_status:
        cobrancas = cobrancas.filter(status=filtro_status)
    return render(
        request,
        "financeiro/cobrancas.html",
        {"cobrancas": cobrancas, "filtro_status": filtro_status, "hoje": date.today()},
    )


def baixa_recebimento(request):
    """Tela de baixa: lança um pagamento e distribui entre as cobranças (docs.md §4.3)."""
    clientes = Cliente.objects.exclude(status=Cliente.Status.INATIVO).order_by("nome")
    cliente = None
    cobrancas = []
    cliente_id = request.GET.get("cliente") or request.POST.get("cliente")
    if cliente_id:
        cliente = get_object_or_404(Cliente, pk=cliente_id)
        cobrancas = list(cobrancas_em_aberto(cliente))

    if request.method == "POST" and cliente and "confirmar" in request.POST:
        try:
            valor = Decimal(request.POST.get("valor", "0").replace(",", "."))
            data_receb = date.fromisoformat(request.POST["data"])
            forma = request.POST["forma"]
            plano = {}
            for cobranca in cobrancas:
                bruto = request.POST.get(f"parcela_{cobranca.id}", "").strip()
                if bruto:
                    plano[cobranca.id] = Decimal(bruto.replace(",", "."))
            _, sobra = registrar_recebimento(
                cliente=cliente,
                valor=valor,
                data=data_receb,
                forma=forma,
                alocacoes_por_cobranca=plano,
                observacoes=request.POST.get("observacoes", ""),
            )
            msg = f"Recebimento de R$ {valor} registrado para {cliente.nome}."
            if sobra > 0:
                msg += f" Saldo não alocado: R$ {sobra} (crédito do cliente)."
            messages.success(request, msg)
            return redirect("financeiro:baixa")
        except (ValidationError, KeyError, InvalidOperation, ValueError) as erro:
            mensagens = getattr(erro, "messages", [str(erro)])
            messages.error(request, "; ".join(mensagens))

    return render(
        request,
        "financeiro/baixa.html",
        {
            "clientes": clientes,
            "cliente": cliente,
            "cobrancas": cobrancas,
            "hoje": date.today(),
            "formas": Recebimento.Forma.choices,
        },
    )


def encargo(request, cobranca_id):
    """Sugere e aplica encargo por atraso (docs.md decisão nº 13), com valor ajustável."""
    cobranca = get_object_or_404(Cobranca, pk=cobranca_id)
    proposta = sugerir_encargo(cobranca)
    if request.method == "POST":
        try:
            valor = Decimal(request.POST.get("valor", "0").replace(",", "."))
            criada = aplicar_encargo(cobranca, valor)
            if criada:
                messages.success(request, f"Encargo de R$ {criada.valor} lançado.")
            else:
                messages.info(request, "Encargo zerado — nada foi cobrado.")
            return redirect("financeiro:cobrancas")
        except (InvalidOperation, ValueError):
            messages.error(request, "Valor inválido.")
    return render(request, "financeiro/encargo.html", {"cobranca": cobranca, "proposta": proposta})


def marcar_judicial(request, cobranca_id):
    cobranca = get_object_or_404(Cobranca, pk=cobranca_id)
    if request.method == "POST":
        cobranca.status = Cobranca.Status.JUDICIAL
        cobranca.save(update_fields=["status"])
        messages.success(request, "Cobrança marcada para cobrança judicial.")
    return redirect("financeiro:cobrancas")


def lista_notas(request):
    notas = NotaDebito.objects.select_related("cliente").prefetch_related("itens")
    return render(request, "financeiro/notas.html", {"notas": notas})


def lista_caucoes(request):
    caucoes = (
        Caucao.objects.exclude(status=Caucao.Status.DEVOLVIDA)
        .select_related("cliente")
        .prefetch_related("movimentacoes")
    )
    return render(request, "financeiro/caucoes.html", {"caucoes": caucoes})


def relatorio_das(request):
    try:
        ano, mes = (request.GET.get("mes") or date.today().strftime("%Y-%m")).split("-")
        referencia = date(int(ano), int(mes), 1)
    except (ValueError, TypeError):
        referencia = date.today().replace(day=1)
    dados = reports.base_das_do_mes(referencia)
    return render(request, "financeiro/das.html", {"dados": dados, "referencia": referencia})


def exportar_das_csv(request):
    """Exportação para a contabilidade (docs.md §5, decisão nº 18)."""
    try:
        ano, mes = (request.GET.get("mes") or date.today().strftime("%Y-%m")).split("-")
        referencia = date(int(ano), int(mes), 1)
    except (ValueError, TypeError):
        referencia = date.today().replace(day=1)
    dados = reports.base_das_do_mes(referencia)

    resposta = HttpResponse(content_type="text/csv; charset=utf-8")
    resposta["Content-Disposition"] = f'attachment; filename="das_{referencia:%Y_%m}.csv"'
    resposta.write("﻿")  # BOM para Excel
    escritor = csv.writer(resposta, delimiter=";")
    escritor.writerow(["Trade Rent a Car — Base do DAS", referencia.strftime("%m/%Y")])
    escritor.writerow([])
    escritor.writerow(["Classe", "Valor recebido (R$)", "Entra no DAS?"])
    escritor.writerow(
        ["Receita de locação", f"{dados['receita_locacao']:.2f}".replace(".", ","), "Sim"]
    )
    escritor.writerow(
        ["Pagamentos diversos", f"{dados['pagamentos_diversos']:.2f}".replace(".", ","), "Não"]
    )
    escritor.writerow([])
    escritor.writerow(["Base de cálculo do DAS", f"{dados['base_das']:.2f}".replace(".", ",")])
    return resposta
