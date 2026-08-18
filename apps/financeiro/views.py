import csv
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import urlencode

from apps.pessoas.models import Cliente

from . import services, whatsapp
from .models import ZERO, Caucao, Cobranca, MovimentacaoCaucao, MovimentoCredito, NotaDebito
from .periodos import ano_mes

#: Alias de compatibilidade — a fonte única dos conjuntos de status é o modelo
#: (Cobranca.STATUS_EM_ABERTO / Cobranca.STATUS_DEVIDOS); não remover sem
#: checar quem importa daqui.
ABERTAS = Cobranca.STATUS_EM_ABERTO

COBRANCAS_POR_PAGINA = 50


def _decimal_br(texto):
    """Decimal aceitando o jeito brasileiro de digitar: '1.250,00' → 1250.00.

    Sem vírgula o texto vale como está ('650' ou '650.00'); com vírgula,
    os pontos são separador de milhar e caem fora.
    """
    texto = (texto or "").strip()
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    return Decimal(texto)


def cobrancas(request):
    """Painel de cobranças com filtros, paginação e sugestão de encargo (docs.md §4.3)."""
    qs = Cobranca.com_quitacao_anotada(
        Cobranca.objects.select_related("cliente", "alocacao__veiculo")
    )
    status = request.GET.get("status", "abertas")
    origem = request.GET.get("origem", "")
    if status == "abertas":
        qs = qs.filter(status__in=Cobranca.STATUS_EM_ABERTO)
    elif status != "todas":
        qs = qs.filter(status=status)
    if origem:
        qs = qs.filter(origem=origem)
    # Em aberto: mais antigas primeiro (cobrar o atraso mais velho); histórico
    # ("todas"/"pago"/"cancelada"): mais recentes primeiro — senão as cobranças
    # da semana ficariam escondidas na última página.
    cronologica = status in ("abertas", "pendente", "parcial", "atrasado", "judicial")
    qs = qs.order_by(*(("vencimento", "pk") if cronologica else ("-vencimento", "-pk")))
    pagina = Paginator(qs, COBRANCAS_POR_PAGINA).get_page(request.GET.get("pagina"))
    hoje = date.today()
    linhas = []
    for cobranca in pagina.object_list:
        encargo = (
            services.sugerir_encargo(cobranca, hoje)
            if cobranca.status == Cobranca.Status.ATRASADO
            and cobranca.origem != Cobranca.Origem.ENCARGO
            and not cobranca.encargos.exclude(status=Cobranca.Status.CANCELADA).exists()
            else None
        )
        cobranca.link_whatsapp = (
            whatsapp.link_cobranca(cobranca, hoje)
            if cobranca.status in Cobranca.STATUS_DEVIDOS
            else ""
        )
        linhas.append((cobranca, encargo))
    return render(
        request,
        "financeiro/cobrancas.html",
        {
            "linhas": linhas,
            "pagina": pagina,
            "filtros": urlencode({"status": status, "origem": origem}),
            "ordem_rotulo": "mais antigas primeiro" if cronologica else "mais recentes primeiro",
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
            valor = _decimal_br(request.POST["valor"])
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


def reabrir_judicial(request, cobranca_id):
    """Volta uma cobrança judicial ao fluxo normal — clique errado tem volta."""
    cobranca = get_object_or_404(Cobranca, pk=cobranca_id)
    if request.method == "POST":
        if cobranca.status != Cobranca.Status.JUDICIAL:
            messages.error(request, "Esta cobrança não está em cobrança judicial.")
        else:
            cobranca.status = Cobranca.Status.PENDENTE
            cobranca.save(update_fields=["status"])
            cobranca.atualizar_status()  # recalcula: pago/parcial/atrasado/pendente
            services.atualizar_inadimplencia(cobranca.cliente)
            messages.success(
                request,
                f"'{cobranca.descricao}' saiu da cobrança judicial "
                f"(status: {cobranca.get_status_display()}).",
            )
    return redirect("financeiro:cobrancas")


#: Origens que o dono pode decidir não cobrar (flexibilidade das decisões nº 13 e da etapa 9).
ORIGENS_CANCELAVEIS = [
    Cobranca.Origem.EXCEDENTE_KM,
    Cobranca.Origem.ENCARGO,
    Cobranca.Origem.OUTRO,
]


def cancelar(request, cobranca_id):
    """Cancela uma cobrança automática que o dono decidiu não cobrar (ex.: excedente de km)."""
    cobranca = get_object_or_404(Cobranca, pk=cobranca_id)
    if request.method == "POST":
        if cobranca.origem not in ORIGENS_CANCELAVEIS:
            messages.error(
                request, "Só cobranças de excedente, encargo ou avulsas podem ser canceladas."
            )
        elif cobranca.status == Cobranca.Status.JUDICIAL:
            messages.error(request, "Cobrança em cobrança judicial não pode ser cancelada.")
        elif cobranca.total_quitado > ZERO:
            messages.error(request, "Cobrança com pagamento aplicado não pode ser cancelada.")
        else:
            cobranca.status = Cobranca.Status.CANCELADA
            cobranca.save(update_fields=["status"])
            messages.success(request, f"Cancelada (não será cobrada): {cobranca.descricao}.")
    return redirect("financeiro:cobrancas")


def receber(request):
    """Baixa de recebimento com travas de lançamento (docs.md §4.3).

    Cobrança judicial também aparece aqui (STATUS_DEVIDOS): acordo pago
    precisa de onde ser lançado. No erro, a tela volta com tudo que foi
    digitado e diz qual campo falhou — nada de redigitar a distribuição.
    """
    clientes = Cliente.objects.order_by("nome")
    cliente = None
    cobrancas_abertas = []
    cliente_id = request.GET.get("cliente") or request.POST.get("cliente")
    if cliente_id:
        cliente = get_object_or_404(Cliente, pk=cliente_id)
        cobrancas_abertas = list(
            cliente.cobrancas.filter(status__in=Cobranca.STATUS_DEVIDOS).order_by("vencimento")
        )
    post = None
    if request.method == "POST" and cliente:
        post = request.POST
        try:
            try:
                valor = _decimal_br(post.get("valor", ""))
            except InvalidOperation:
                raise ValidationError(
                    "Valor recebido inválido — digite só números, com vírgula "
                    "para os centavos (ex.: 1250,00)."
                ) from None
            try:
                data = date.fromisoformat(post.get("data", ""))
            except ValueError:
                raise ValidationError("Data do recebimento inválida.") from None
            aplicacoes = []
            for cobranca in cobrancas_abertas:
                bruto = post.get(f"aplicar_{cobranca.pk}", "").strip()
                if not bruto:
                    continue
                try:
                    aplicacoes.append((cobranca, _decimal_br(bruto)))
                except InvalidOperation:
                    raise ValidationError(
                        f"Valor a aplicar inválido em '{cobranca.descricao}' (ex.: 650,00)."
                    ) from None
            services.registrar_recebimento(
                cliente=cliente,
                data=data,
                valor=valor,
                forma=post.get("forma", "pix"),
                aplicacoes=aplicacoes,
                sobra_destino=post.get("sobra_destino", "credito"),
                observacoes=post.get("observacoes", ""),
            )
            messages.success(request, f"Recebimento de R$ {valor} lançado para {cliente.nome}.")
            return redirect(f"{request.path}?cliente={cliente.pk}")
        except ValidationError as erro:
            messages.error(request, "; ".join(erro.messages))
    credito = MovimentoCredito.saldo_do_cliente(cliente) if cliente else ZERO
    linhas = [
        (cobranca, post.get(f"aplicar_{cobranca.pk}", "") if post else "")
        for cobranca in cobrancas_abertas
    ]
    return render(
        request,
        "financeiro/receber.html",
        {
            "clientes": clientes,
            "cliente": cliente,
            "linhas": linhas,
            "credito": credito,
            "hoje": date.today(),
            "post": post,
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
    """Emissão de ND idempotente: token único por formulário barra o clique duplo,
    e o erro devolve a tela com os itens digitados preservados."""
    clientes = Cliente.objects.order_by("nome")
    post = None
    if request.method == "POST":
        post = request.POST
        token = post.get("token", "")
        usados = request.session.get("nd_tokens_usados", [])
        if token and token in usados:
            # segunda submissão do mesmo formulário: a ND já foi emitida
            messages.info(request, "Nota de débito já emitida — clique duplo ignorado.")
            return redirect("financeiro:nds")
        try:
            cliente = get_object_or_404(Cliente, pk=post["cliente"])
            descricoes = post.getlist("item_descricao")
            valores = post.getlist("item_valor")
            itens = [
                (descricao.strip(), _decimal_br(valor))
                for descricao, valor in zip(descricoes, valores, strict=False)
                if descricao.strip() and valor.strip()
            ]
            nd = services.emitir_nota_debito(
                cliente=cliente,
                data_emissao=date.fromisoformat(post["data_emissao"]),
                vencimento=(
                    date.fromisoformat(post["vencimento"]) if post.get("vencimento") else None
                ),
                itens=itens,
                observacoes=post.get("observacoes", ""),
            )
            if token:
                request.session["nd_tokens_usados"] = (usados + [token])[-20:]
            messages.success(request, f"ND {nd.numero:03d} emitida (R$ {nd.total}).")
            return redirect("financeiro:nds")
        except ValidationError as erro:
            messages.error(request, "; ".join(erro.messages))
        except (KeyError, InvalidOperation, ValueError):
            messages.error(request, "Preencha cliente, data e itens corretamente.")
    itens_iniciais = [{"d": "", "v": ""}]
    if post:
        digitados = [
            {"d": descricao, "v": valor}
            for descricao, valor in zip(
                post.getlist("item_descricao"), post.getlist("item_valor"), strict=False
            )
        ]
        itens_iniciais = digitados or itens_iniciais
    return render(
        request,
        "financeiro/nd_nova.html",
        {
            "clientes": clientes,
            "hoje": date.today(),
            "post": post,
            "itens_iniciais": itens_iniciais,
            "token": uuid4().hex,
        },
    )


def caucoes(request):
    lista = Caucao.com_saldos_anotados(
        Caucao.objects.select_related("alocacao__cliente", "alocacao__veiculo")
    )
    return render(request, "financeiro/caucoes.html", {"caucoes": lista})


def caucao_detalhe(request, caucao_id):
    caucao = get_object_or_404(Caucao, pk=caucao_id)
    cliente = caucao.alocacao.cliente
    # judicial entra: acordo pode ser quitado descontando da caução retida
    pendentes = cliente.cobrancas.filter(status__in=Cobranca.STATUS_DEVIDOS).order_by("vencimento")
    if request.method == "POST":
        try:
            tipo = request.POST["tipo"]
            valor = _decimal_br(request.POST["valor"])
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


def das(request):
    """Base de cálculo do DAS — só receita de locação é tributável (decisão nº 11)."""
    ano, mes = ano_mes(request)
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
