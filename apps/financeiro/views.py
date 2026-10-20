import csv
from datetime import date
from decimal import InvalidOperation
from uuid import uuid4

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Exists, OuterRef, Prefetch
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import urlencode

from apps.campos import dinheiro_br, exibir_dinheiro
from apps.formatos import brl
from apps.pessoas.models import Cliente

from . import recibo, services, whatsapp
from .models import (
    ZERO,
    Caucao,
    Cobranca,
    MovimentacaoCaucao,
    MovimentoCredito,
    NotaDebito,
    Recebimento,
)
from .periodos import ano_mes

COBRANCAS_POR_PAGINA = 50

#: Origens que o dono pode decidir não cobrar: excedente de km, encargo por
#: atraso (decisão nº 13, "ajustável caso a caso") e cobranças avulsas.
ORIGENS_CANCELAVEIS = [
    Cobranca.Origem.EXCEDENTE_KM,
    Cobranca.Origem.ENCARGO,
    Cobranca.Origem.OUTRO,
]


def _cobrancas_devidas(cliente):
    """O que o cliente ainda deve (judicial inclusa), com o saldo anotado em lote."""
    return list(
        Cobranca.com_quitacao_anotada(
            cliente.cobrancas.filter(status__in=Cobranca.STATUS_DEVIDOS).order_by("vencimento")
        )
    )


def _abertas_por_cliente(cliente_ids):
    """{cliente_id: [cobranças no fluxo normal com saldo]} numa consulta só.

    Alimenta a mensagem única de WhatsApp por cliente — judicial fica de fora.
    """
    abertas = {}
    qs = Cobranca.com_quitacao_anotada(
        Cobranca.objects.filter(
            cliente_id__in=cliente_ids, status__in=Cobranca.STATUS_EM_ABERTO
        ).select_related("cliente", "alocacao__veiculo")
    ).order_by("vencimento", "pk")
    for cobranca in qs:
        if cobranca.saldo > ZERO:
            abertas.setdefault(cobranca.cliente_id, []).append(cobranca)
    return abertas


def cobrancas(request):
    """Painel de cobranças com filtros, paginação e sugestão de encargo (docs.md §4.3)."""
    encargo_ativo = Cobranca.objects.filter(cobranca_origem=OuterRef("pk")).exclude(
        status=Cobranca.Status.CANCELADA
    )
    qs = Cobranca.com_quitacao_anotada(
        Cobranca.objects.select_related("cliente", "alocacao__veiculo")
    ).annotate(tem_encargo_ativo=Exists(encargo_ativo))
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
    abertas_por_cliente = _abertas_por_cliente({c.cliente_id for c in pagina.object_list})
    clientes_com_link = set()
    linhas = []
    for cobranca in pagina.object_list:
        encargo = (
            services.sugerir_encargo(cobranca, hoje)
            if cobranca.status == Cobranca.Status.ATRASADO
            and cobranca.origem != Cobranca.Origem.ENCARGO
            and not cobranca.tem_encargo_ativo
            else None
        )
        # judicial/quitada saem vazias (whatsapp._cobravel)
        cobranca.link_whatsapp = whatsapp.link_cobranca(cobranca, hoje)
        # "cobrar tudo": uma mensagem com todos os itens, na 1ª linha cobrável do
        # cliente — nunca numa linha judicial/paga (lá o aviso é "sem WhatsApp")
        abertas = abertas_por_cliente.get(cobranca.cliente_id, [])
        cobranca.link_whatsapp_cliente = ""
        cobranca.qtd_abertas_cliente = len(abertas)
        if (
            cobranca.link_whatsapp
            and len(abertas) > 1
            and cobranca.cliente_id not in clientes_com_link
        ):
            clientes_com_link.add(cobranca.cliente_id)
            cobranca.link_whatsapp_cliente = whatsapp.link_cliente(cobranca.cliente, abertas, hoje)
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
            "origens_cancelaveis": ORIGENS_CANCELAVEIS,
        },
    )


def aplicar_encargo(request, cobranca_id):
    cobranca = get_object_or_404(Cobranca, pk=cobranca_id)
    if request.method == "POST":
        try:
            valor = dinheiro_br(request.POST["valor"])
            services.aplicar_encargo(cobranca, valor)
            messages.success(request, f"Encargo de {brl(valor)} aplicado.")
        except (ValidationError, KeyError, InvalidOperation) as erro:
            messages.error(
                request,
                "; ".join(
                    getattr(
                        erro,
                        "messages",
                        ["Valor do encargo inválido — digite só números (ex.: 45,00)."],
                    )
                ),
            )
    return redirect("financeiro:cobrancas")


def marcar_judicial(request, cobranca_id):
    cobranca = get_object_or_404(Cobranca, pk=cobranca_id)
    if request.method == "POST":
        if cobranca.status not in Cobranca.STATUS_EM_ABERTO:
            # paga/cancelada/já judicial: não reabrir dívida encerrada por clique repetido
            messages.error(request, "Só cobrança em aberto pode ir para cobrança judicial.")
            return redirect("financeiro:cobrancas")
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


def _id_valido(texto):
    """pk vinda da URL: só algarismos ASCII e de tamanho sensato.

    str.isdigit() aceita "²" e números gigantes estouram o inteiro do banco —
    os dois virariam erro 500 em vez de serem ignorados.
    """
    return bool(texto) and texto.isascii() and texto.isdigit() and len(texto) <= 9


def _resumo_do_recibo(request, cliente):
    """?recibo=<pk>: o aviso pós-lançamento (o que quitou, sobra, recibo, desfazer)."""
    recibo_id = request.GET.get("recibo", "")
    if not (cliente and _id_valido(recibo_id)):
        return None
    recebimento = (
        services.anotar_lancamento(Recebimento.objects.filter(pk=recibo_id, cliente=cliente))
        .select_related("cliente")
        .first()
    )
    if recebimento is None:  # desfeito ou de outro cliente: some sem erro
        return None
    resumo = services.resumo_recebimento(recebimento)
    resumo["link_whatsapp"] = recibo.link_recibo(resumo)
    resumo["pode_desfazer"] = services.pode_desfazer(recebimento)
    return resumo


def receber(request):
    """Baixa de recebimento com travas de lançamento (docs.md §4.3).

    Cobrança judicial também aparece aqui (STATUS_DEVIDOS): acordo pago
    precisa de onde ser lançado. No erro, a tela volta com tudo que foi
    digitado e diz qual campo falhou — nada de redigitar a distribuição.

    Atalhos (botões "Recebi"): ?cliente=<pk> já escolhe o cliente e
    ?cobranca=<pk> já preenche o valor com o que falta daquela cobrança.
    Depois de lançar, ?recibo=<pk> mostra o resumo, o recibo e o desfazer.
    """
    clientes = Cliente.objects.order_by("nome")
    cliente = None
    cobrancas_abertas = []
    cliente_id = request.GET.get("cliente") or request.POST.get("cliente")
    if cliente_id:
        if not _id_valido(cliente_id):
            raise Http404("Cliente inválido.")
        cliente = get_object_or_404(Cliente, pk=cliente_id)
        cobrancas_abertas = _cobrancas_devidas(cliente)
    post = None
    if request.method == "POST" and cliente:
        post = request.POST
        try:
            try:
                valor = dinheiro_br(post.get("valor", ""))
            except InvalidOperation:
                raise ValidationError(
                    "Valor recebido inválido — digite só números, com vírgula "
                    "para os centavos (ex.: 1250,00)."
                ) from None
            try:
                data = date.fromisoformat(post.get("data", ""))
            except ValueError:
                raise ValidationError(
                    "Data do recebimento inválida — escolha a data no calendário."
                ) from None
            aplicacoes = []
            for cobranca in cobrancas_abertas:
                bruto = post.get(f"aplicar_{cobranca.pk}", "").strip()
                if not bruto:
                    continue
                try:
                    aplicacoes.append((cobranca, dinheiro_br(bruto)))
                except InvalidOperation:
                    raise ValidationError(
                        f"Valor a aplicar inválido em '{cobranca.descricao}' (ex.: 650,00)."
                    ) from None
            recebimento = services.registrar_recebimento(
                cliente=cliente,
                data=data,
                valor=valor,
                forma=post.get("forma", "pix"),
                aplicacoes=aplicacoes,
                sobra_destino=post.get("sobra_destino", "credito"),
                observacoes=post.get("observacoes", ""),
                bloquear_repetido=True,
            )
            messages.success(request, f"Recebimento de {brl(valor)} lançado para {cliente.nome}.")
            return redirect(f"{request.path}?cliente={cliente.pk}&recibo={recebimento.pk}")
        except services.RecebimentoRepetido as repetido:
            # toque duplo: mostra o resumo do lançamento que já valeu
            messages.warning(request, repetido.messages[0])
            return redirect(f"{request.path}?cliente={cliente.pk}&recibo={repetido.recebimento.pk}")
        except ValidationError as erro:
            messages.error(request, "; ".join(erro.messages))
            # a transação foi desfeita, mas os objetos em memória podem ter status
            # de uma aplicação que não valeu: a tela volta com o que está no banco
            cobrancas_abertas = _cobrancas_devidas(cliente)
    elif cliente and _id_valido(request.GET.get("cobranca", "")):
        # "Recebi" de uma cobrança: valor = o que falta dela, já aplicado nela
        escolhida = next(
            (c for c in cobrancas_abertas if str(c.pk) == request.GET["cobranca"]), None
        )
        if escolhida and escolhida.saldo > ZERO:
            saldo = exibir_dinheiro(escolhida.saldo)
            post = {"valor": saldo, f"aplicar_{escolhida.pk}": saldo}
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
            "devido": sum((c.saldo for c in cobrancas_abertas), ZERO),
            "hoje": date.today(),
            "post": post,
            # crédito não é forma de pagamento: tem botão próprio na tela
            "formas": [f for f in Recebimento.Forma.choices if f[0] != Recebimento.Forma.CREDITO],
            "resumo": _resumo_do_recibo(request, cliente),
        },
    )


def desfazer_recebimento(request, recebimento_id):
    """Botão "Desfazer este lançamento" — só no mesmo dia (services.pode_desfazer)."""
    recebimento = Recebimento.objects.select_related("cliente").filter(pk=recebimento_id).first()
    if recebimento is None:
        # toque duplo no Desfazer (ou outra aba): o 1º já apagou — avisa em vez de "página
        # não encontrada"; o histórico ainda sabe de qual cliente era
        apagado = Recebimento.history.filter(id=recebimento_id).order_by("-history_date").first()
        if apagado is None:
            raise Http404("Lançamento não encontrado.")
        messages.info(request, "Este lançamento já foi desfeito.")
        return redirect(f"{reverse('financeiro:receber')}?cliente={apagado.cliente_id}")
    cliente = recebimento.cliente
    if request.method != "POST":
        return redirect(f"{reverse('financeiro:receber')}?cliente={cliente.pk}")
    valor = recebimento.valor
    try:
        services.desfazer_recebimento(recebimento)
    except ValidationError as erro:
        messages.error(request, "; ".join(erro.messages))
        return redirect(
            f"{reverse('financeiro:receber')}?cliente={cliente.pk}&recibo={recebimento.pk}"
        )
    messages.success(
        request,
        f"Lançamento de {brl(valor)} de {cliente.nome} desfeito — as cobranças voltaram "
        "a ficar em aberto. Se precisar, lance de novo com os dados certos.",
    )
    return redirect(f"{reverse('financeiro:receber')}?cliente={cliente.pk}")


def nds(request):
    # cobrança com o quitado anotado: o botão "Enviar ao motorista" olha o saldo
    # de cada ND sem 2 consultas por linha
    notas = list(
        NotaDebito.objects.select_related("cliente").prefetch_related(
            "itens", Prefetch("cobranca", queryset=Cobranca.com_quitacao_anotada())
        )
    )
    hoje = date.today()
    for nd in notas:
        nd.link_whatsapp = whatsapp.link_nd(nd, hoje)
        # cobrável mas sem telefone: avisa em vez de sumir com o botão sem explicação
        cobranca = getattr(nd, "cobranca", None)
        nd.sem_telefone = (
            not nd.link_whatsapp
            and cobranca is not None
            and cobranca.status in Cobranca.STATUS_EM_ABERTO
            and cobranca.saldo > ZERO
            and not whatsapp.telefone_whatsapp(nd.cliente.telefone)
        )
    return render(request, "financeiro/nds.html", {"notas": notas, "hoje": hoje})


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
            messages.warning(
                request,
                "Esta nota de débito já tinha sido emitida — o clique duplo foi ignorado "
                "e nada foi cobrado em dobro.",
            )
            return redirect("financeiro:nds")
        try:
            cliente = get_object_or_404(Cliente, pk=post["cliente"])
            descricoes = post.getlist("item_descricao")
            valores = post.getlist("item_valor")
            itens = [
                (descricao.strip(), dinheiro_br(valor))
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
            messages.success(request, f"ND {nd.numero:03d} emitida ({brl(nd.total)}).")
            return redirect("financeiro:nds")
        except ValidationError as erro:
            messages.error(request, "; ".join(erro.messages))
        except (KeyError, InvalidOperation, ValueError):
            messages.error(
                request,
                "Não deu para emitir: escolha o cliente, confira as datas de emissão e de "
                "vencimento e "
                "preencha ao menos um item com descrição e valor (ex.: 150,00).",
            )
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
            # o formulário calcula o vencimento sugerido ao trocar cliente/emissão
            "dias_pagamento": {
                str(pk): dia for pk, dia in services.dias_pagamento_por_cliente().items()
            },
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
    caucao = get_object_or_404(
        Caucao.com_saldos_anotados(
            Caucao.objects.select_related("alocacao__cliente", "alocacao__veiculo")
        ),
        pk=caucao_id,
    )
    cliente = caucao.alocacao.cliente
    if request.method == "POST":
        try:
            tipo = request.POST["tipo"]
            if tipo not in MovimentacaoCaucao.Tipo.values:
                raise ValidationError('Escolha em "O que aconteceu?" o tipo da movimentação.')
            valor = dinheiro_br(request.POST["valor"])
            if valor <= ZERO:
                raise ValidationError("O valor deve ser maior que zero.")
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
                    raise ValidationError(f"Devolução acima do saldo ({brl(caucao.saldo)}).")
                MovimentacaoCaucao.objects.create(
                    caucao=caucao,
                    tipo=tipo,
                    valor=valor,
                    data=data,
                    forma=request.POST.get("forma", ""),
                    observacoes=request.POST.get("observacoes", ""),
                )
            messages.success(
                request, f"Movimentação de {brl(valor)} registrada na caução de {cliente.nome}."
            )
            return redirect("financeiro:caucao_detalhe", caucao.pk)
        except ValidationError as erro:
            messages.error(request, "; ".join(erro.messages))
        except (KeyError, InvalidOperation, ValueError):
            messages.error(
                request,
                "Não deu para registrar: confira o valor (ex.: 1.500,00), a data e, "
                "se for desconto, escolha a cobrança a quitar.",
            )
    return render(
        request,
        "financeiro/caucao_detalhe.html",
        {
            "caucao": caucao,
            "movimentacoes": caucao.movimentacoes.select_related("cobranca"),
            # judicial entra: acordo pode ser quitado descontando da caução retida
            "pendentes": _cobrancas_devidas(cliente),
            "hoje": date.today(),
        },
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
    recebimentos = list(
        services.anotar_lancamento(cliente.recebimentos.all()).prefetch_related(
            "aplicacoes__cobranca"
        )[:50]
    )
    for recebimento in recebimentos:  # lançado hoje: tem o botão "Desfazer"
        recebimento.pode_desfazer = services.pode_desfazer(recebimento)
    return render(
        request,
        "financeiro/extrato_cliente.html",
        {
            "cliente": cliente,
            "cobrancas": Cobranca.com_quitacao_anotada(cliente.cobrancas.order_by("-vencimento"))[
                :100
            ],
            "recebimentos": recebimentos,
            "status_devidos": Cobranca.STATUS_DEVIDOS,  # quem ganha o botão "Recebi"
            "credito": MovimentoCredito.saldo_do_cliente(cliente),
            # mensagem única com tudo em aberto (judicial fica de fora)
            "link_whatsapp": whatsapp.link_cliente(
                cliente, _abertas_por_cliente([cliente.pk]).get(cliente.pk, []), date.today()
            ),
        },
    )
