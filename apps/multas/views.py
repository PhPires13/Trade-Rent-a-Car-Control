from datetime import date

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import urlencode
from django.views.decorators.cache import never_cache

from apps.alocacoes.services import cliente_vigente
from apps.campos import CampoDinheiro
from apps.financeiro import services as financeiro_services
from apps.formatos import brl
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente

from . import services
from .forms import MultaEdicaoForm, MultaForm, OrgaoForm, condutores_do_cliente
from .models import Multa, OrgaoAutuador


def _valor_digitado(request):
    """Valor opcional do POST ("1.250,00"), validado como o campo `Multa.valor`.

    Só o dinheiro_br aceitava "99999999" ou "1e10": o banco recusava (ou gravava
    um número que depois derrubava a lista). Vazio → None.
    """
    campo = Multa._meta.get_field("valor").formfield(form_class=CampoDinheiro, min_value=0)
    return campo.clean(request.POST.get("valor", "").strip())


def _volta(request):
    """Filtros da lista para voltar a ela depois de uma ação (sem perder a busca).

    Vem como query string no campo "volta"; reconstruída pelo QueryDict, nunca
    vira outro endereço — só parâmetros da própria lista de multas.
    """
    volta = (request.POST.get("volta") or request.GET.get("volta") or "").lstrip("?")
    return QueryDict(volta).urlencode()


def _voltar_para_lista(request, ancora=""):
    destino = reverse("multas:lista")
    volta = _volta(request)
    if volta:
        destino += f"?{volta}"
    return redirect(destino + ancora)


def lista(request):
    hoje = date.today()
    multas = Multa.objects.select_related("veiculo", "cliente", "orgao").prefetch_related(
        Multa.PREFETCH_REPASSE
    )
    veiculo = request.GET.get("veiculo", "").strip()
    if veiculo:
        multas = multas.filter(veiculo__placa__icontains=veiculo)
    cliente = None
    cliente_id = request.GET.get("cliente", "")
    if cliente_id.isdigit():
        cliente = Cliente.objects.filter(pk=cliente_id).first()
        if cliente:
            multas = multas.filter(cliente=cliente)
    resultado = request.GET.get("resultado")
    if resultado:
        multas = multas.filter(resultado=resultado)
    # contadores dos atalhos respeitam placa/cliente/resultado, mas não o próprio atalho
    contadores = services.contar_tarefas(multas, hoje)
    tarefa = request.GET.get("tarefa", "")
    if tarefa not in dict(services.TAREFAS):
        tarefa = ""
    multas = services.filtrar_tarefa(multas, tarefa)
    pagina = Paginator(multas, 50).get_page(request.GET.get("pagina"))
    filtros = {
        "veiculo": veiculo,
        "cliente": cliente.pk if cliente else "",
        "resultado": resultado or "",
        "tarefa": tarefa,
    }
    filtros_sem_tarefa = urlencode({k: v for k, v in filtros.items() if v and k != "tarefa"})
    atalhos = [
        {
            "chave": chave,
            "rotulo": rotulo,
            "total": contadores[chave],
            "vencidas": contadores["indicar_vencidas"] if chave == "indicar" else 0,
            "ativo": tarefa == chave,
        }
        for chave, rotulo in services.TAREFAS
    ]
    return render(
        request,
        "multas/lista.html",
        {
            "multas": pagina.object_list,
            "pagina": pagina,
            "filtros": urlencode(filtros),
            "filtros_sem_tarefa": filtros_sem_tarefa,
            "volta": request.GET.urlencode(),
            "atalhos": atalhos,
            "tarefa": tarefa,
            "resultados": Multa.Resultado.choices,
            "filtro_veiculo": veiculo,
            "filtro_cliente": cliente,
            "filtro_resultado": resultado or "",
            "hoje": hoje,
        },
    )


def nova(request):
    form = MultaForm(request.POST or None, initial={"data_infracao": date.today()})
    if request.method == "POST" and form.is_valid():
        multa = form.save()
        nome = multa.cliente.nome if multa.cliente else "nenhum cliente na data"
        messages.success(request, f"Multa registrada — atribuída a: {nome}.")
        return redirect("multas:lista")
    return render(request, "multas/nova.html", {"form": form, "multa": None})


def editar(request, multa_id):
    """Página de editar a multa com tudo o que muda depois: resultado, FICI, pagamento.

    Antes o único caminho era o /admin (auditoria UX #10). Abaixo do formulário,
    um histórico simples de quem mudou o quê.
    """
    multa = get_object_or_404(Multa.objects.select_related("veiculo", "cliente"), pk=multa_id)
    form = MultaEdicaoForm(request.POST or None, instance=multa)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Multa {multa.veiculo.placa} atualizada.")
        return _voltar_para_lista(request)
    return render(
        request,
        "multas/nova.html",
        {
            "form": form,
            "multa": multa,
            "volta": _volta(request),
            "historico": services.historico(multa),
        },
    )


def quem_estava(request):
    """Trecho HTMX do formulário: com quem o carro estava na data da infração.

    Também devolve (fora do lugar, hx-swap-oob) a lista de "nome de quem
    dirigia" só com os condutores daquele cliente.
    """
    veiculo = None
    if request.GET.get("veiculo", "").isdigit():
        veiculo = Veiculo.objects.filter(pk=request.GET["veiculo"]).first()
    try:
        data = date.fromisoformat(request.GET.get("data_infracao", ""))
    except ValueError:
        data = None
    vigente = cliente_vigente(veiculo, data) if veiculo and data else None
    escolhido = None
    if request.GET.get("cliente", "").isdigit():
        escolhido = Cliente.objects.filter(pk=request.GET["cliente"]).first()
    atual = request.GET.get("condutor_autorizado", "")
    atual = int(atual) if atual.isdigit() else None
    form = MultaForm(initial={"condutor_autorizado": atual})
    form.fields["condutor_autorizado"].queryset = condutores_do_cliente(escolhido or vigente, atual)
    return render(
        request,
        "multas/_quem_estava.html",
        {
            "veiculo": veiculo,
            "data": data,
            "vigente": vigente,
            "escolhido": escolhido,
            "campo_condutor": form["condutor_autorizado"],
        },
    )


def indicar_fici(request, multa_id):
    multa = get_object_or_404(Multa, pk=multa_id)
    if request.method == "POST" and multa.fici_status != Multa.Fici.PENDENTE:
        # aba antiga/clique repetido: não desfaz uma NIC nem troca a data já gravada
        messages.info(
            request,
            f"A indicação do motorista desta multa já está como "
            f'"{multa.get_fici_status_display()}".',
        )
    elif request.method == "POST":
        multa.fici_status = Multa.Fici.INDICADO
        multa.fici_data_indicacao = date.today()
        multa.save(update_fields=["fici_status", "fici_data_indicacao"])
        messages.success(request, f"FICI indicado para {multa}.")
    return _voltar_para_lista(request)


def registrar_nic(request, multa_id):
    multa = get_object_or_404(Multa, pk=multa_id)
    if request.method == "POST":
        if multa.fici_status == Multa.Fici.INDICADO:
            # aba antiga: o motorista já foi indicado, não há NIC a registrar
            messages.error(request, "O motorista desta multa já foi indicado — não há NIC.")
            return _voltar_para_lista(request)
        if multa.fici_prazo and multa.fici_prazo >= date.today():
            # dentro do prazo ainda dá para indicar — a NIC criaria multa à toa (UX #13)
            messages.error(
                request,
                f"O prazo para indicar o motorista vai até {multa.fici_prazo:%d/%m/%Y}. "
                "Indique o condutor; a NIC só se registra depois que o prazo vence.",
            )
            return _voltar_para_lista(request)
        try:
            nic = services.registrar_nic(multa, valor=_valor_digitado(request))
            messages.warning(request, f"NIC registrada contra a empresa: {nic}.")
        except ValidationError as erro:
            messages.error(request, "; ".join(erro.messages))
    return _voltar_para_lista(request)


def marcar_paga(request, multa_id):
    multa = get_object_or_404(Multa, pk=multa_id)
    if request.method == "POST":
        multa.pagamento = Multa.Pagamento.PAGO
        multa.pago_por = request.POST.get("pago_por", "").strip()[:60]
        multa.save(update_fields=["pagamento", "pago_por"])
        quem = {"Empresa": " pela empresa", "Cliente": " pelo cliente"}.get(multa.pago_por, "")
        messages.success(request, f"Multa marcada como paga{quem}.")
    return _voltar_para_lista(request)


def gerar_nd(request):
    """Seleciona multas 'a cobrar' de um cliente e emite a ND (docs.md §4.7)."""
    clientes = Cliente.objects.order_by("nome")
    cliente = None
    a_cobrar = []
    cliente_id = request.GET.get("cliente") or request.POST.get("cliente")
    if cliente_id:
        if not cliente_id.isdigit():  # link adulterado dava erro 500 no filtro por pk
            raise Http404("Cliente inválido.")
        cliente = get_object_or_404(Cliente, pk=cliente_id)
        candidatas = cliente.multas.select_related("veiculo").prefetch_related(
            Multa.PREFETCH_REPASSE
        )
        a_cobrar = [m for m in candidatas if m.repasse == "A cobrar"]
    vencimento = _vencimento_nd(cliente, request) if cliente else None
    if request.method == "POST" and cliente:
        marcadas = [m.pk for m in a_cobrar if str(m.pk) in request.POST.getlist("multa")]
        # recarrega sem o prefetch: o serviço reconfere `repasse` no banco, dentro da
        # transação — com os itens em memória, dois envios seguidos gerariam ND em dobro
        selecionadas = list(cliente.multas.select_related("veiculo").filter(pk__in=marcadas))
        try:
            if vencimento["data"] is None:
                raise ValidationError("Vencimento inválido — use uma data a partir de hoje.")
            nd = services.gerar_nd_de_multas(
                cliente, selecionadas, date.today(), vencimento=vencimento["data"]
            )
            messages.success(request, f"ND {nd.numero:03d} emitida ({brl(nd.total)}).")
            return redirect("financeiro:nds")
        except ValidationError as erro:
            messages.error(request, "; ".join(erro.messages))
    return render(
        request,
        "multas/gerar_nd.html",
        {"clientes": clientes, "cliente": cliente, "multas": a_cobrar, "vencimento": vencimento},
    )


def _vencimento_nd(cliente, request):
    """Vencimento da ND de multas (proposta #6, opção A).

    Padrão: o próximo dia de pagamento do cliente — nunca hoje, senão amanhã
    ele vira inadimplente sozinho. A pessoa pode trocar a data no formulário;
    data inválida ou antes de hoje fica em "data" = None (o POST mostra o erro).
    """
    hoje = date.today()
    dia = financeiro_services.dia_pagamento(cliente)
    sugerido = financeiro_services.proximo_dia_pagamento(cliente, hoje, dia=dia)
    if dia is None:
        explicacao = "o cliente ainda não tem dia de pagamento, então são 7 dias a partir de hoje"
    else:
        explicacao = "é o próximo dia de pagamento do cliente"
    data = sugerido
    digitado = request.POST.get("vencimento", "") if request.method == "POST" else ""
    if digitado:
        try:
            data = date.fromisoformat(digitado)
        except ValueError:
            data = None
        if data is not None and data < hoje:
            data = None
    return {"data": data, "sugerido": sugerido, "explicacao": explicacao, "digitado": digitado}


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
