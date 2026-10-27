from datetime import date
from uuid import uuid4

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.cache import never_cache

from apps.alocacoes.services import cliente_vigente
from apps.campos import CampoDinheiro, id_valido, ler_data
from apps.financeiro import services as financeiro_services
from apps.financeiro.views import _envio_unico
from apps.formatos import brl
from apps.frota.models import Veiculo, normalizar_placa
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
    hoje = timezone.localdate()
    multas = Multa.objects.select_related("veiculo", "cliente", "orgao").prefetch_related(
        Multa.PREFETCH_REPASSE
    )
    veiculo = request.GET.get("veiculo", "").strip()
    if veiculo:
        # o banco grava a placa sem hífen/espaço: "ABC-1D23" precisa achar "ABC1D23"
        multas = multas.filter(veiculo__placa__icontains=normalizar_placa(veiculo))
    cliente = None
    cliente_id = request.GET.get("cliente", "")
    if id_valido(cliente_id):
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
        # "Definir responsável" só aparece quando há o que definir
        if chave != "definir" or contadores[chave] or tarefa == chave
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


#: chave da sessão com os códigos de formulário de "Nova multa" já gravados
SESSAO_TOKENS_MULTA = "multa_tokens_usados"


def nova(request):
    """Cadastro de multa. Cada formulário leva um código único (como a ND):
    reenviar o mesmo formulário (internet lenta, Voltar, duas abas) não grava
    a multa duas vezes — senão o cliente acabava cobrado em dobro."""
    token = request.POST.get("token", "") if request.method == "POST" else ""
    form = MultaForm(request.POST or None, initial={"data_infracao": timezone.localdate()})
    if request.method == "POST" and form.is_valid():
        # mesma trava da ND: a linha da sessão fica travada até gravar, então o 2º
        # envio de um clique duplo com internet lenta espera e vê o código já usado
        with _envio_unico(request, SESSAO_TOKENS_MULTA, token) as repetido:
            if repetido:
                messages.warning(
                    request,
                    "Esta multa já tinha sido registrada — o envio repetido foi ignorado e "
                    "nada foi gravado em dobro.",
                )
                return redirect("multas:lista")
            multa = form.save()
        nome = multa.cliente.nome if multa.cliente else "nenhum cliente na data"
        messages.success(request, f"Multa registrada — atribuída a: {nome}.")
        return redirect("multas:lista")
    return render(
        request,
        "multas/nova.html",
        {"form": form, "multa": None, "token": token or uuid4().hex},
    )


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
    if id_valido(request.GET.get("veiculo", "")):
        veiculo = Veiculo.objects.filter(pk=request.GET["veiculo"]).first()
    try:
        data = date.fromisoformat(request.GET.get("data_infracao", ""))
    except ValueError:
        data = None
    vigente = cliente_vigente(veiculo, data) if veiculo and data else None
    escolhido = None
    if id_valido(request.GET.get("cliente", "")):
        escolhido = Cliente.objects.filter(pk=request.GET["cliente"]).first()
    atual = request.GET.get("condutor_autorizado", "")
    atual = int(atual) if id_valido(atual) else None
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
    if request.method == "POST" and multa.anulada:
        messages.info(
            request,
            f"Esta multa está como “{multa.get_resultado_display()}” — não há condutor a indicar.",
        )
    elif request.method == "POST" and multa.fici_status != Multa.Fici.PENDENTE:
        # aba antiga/clique repetido: não desfaz uma NIC nem troca a data já gravada
        messages.info(
            request,
            f"A indicação do motorista desta multa já está como "
            f'"{multa.get_fici_status_display()}".',
        )
    elif request.method == "POST":
        multa.fici_status = Multa.Fici.INDICADO
        multa.fici_data_indicacao = timezone.localdate()
        multa.save(update_fields=["fici_status", "fici_data_indicacao"])
        messages.success(request, f"FICI indicado para {multa}.")
    return _voltar_para_lista(request)


def registrar_nic(request, multa_id):
    multa = get_object_or_404(Multa, pk=multa_id)
    if request.method == "POST":
        if multa.anulada:
            # cancelada / advertência / não exigível: não há condutor a indicar nem NIC
            messages.info(
                request,
                f"Esta multa está como “{multa.get_resultado_display()}” — não há NIC a registrar.",
            )
            return _voltar_para_lista(request)
        if multa.fici_status == Multa.Fici.INDICADO:
            # aba antiga: o motorista já foi indicado, não há NIC a registrar
            messages.info(request, "O motorista desta multa já foi indicado — não há NIC.")
            return _voltar_para_lista(request)
        if multa.fici_status != Multa.Fici.PENDENTE:
            # aba antiga, NIC já registrada ou multa que já é uma NIC (NIC de NIC)
            messages.info(
                request,
                "A indicação do motorista desta multa já está como "
                f"“{multa.get_fici_status_display()}” — não há NIC a registrar.",
            )
            return _voltar_para_lista(request)
        if multa.fici_prazo and multa.fici_prazo >= timezone.localdate():
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
        # só as duas opções do botão (D23): texto solto não tirava a multa da ND
        digitado = request.POST.get("pago_por", "").strip().lower()
        multa.pago_por = {"empresa": "Empresa", "cliente": Multa.PAGO_POR_CLIENTE}.get(digitado, "")
        multa.save(update_fields=["pagamento", "pago_por"])
        quem = {"Empresa": " pela empresa", "Cliente": " pelo cliente"}.get(multa.pago_por, "")
        aviso = ""
        if multa.paga_pelo_cliente:
            item = multa.itens_nd.select_related("nota_debito").first()
            if item is None:
                aviso = " Ela não entra mais na nota de débito: o cliente já pagou ao órgão."
            else:
                aviso = (
                    f" Atenção: ela já está na ND {item.nota_debito.numero:03d} — se o cliente "
                    "pagou direto ao órgão, ajuste a nota para não cobrar duas vezes."
                )
        messages.success(request, f"Multa marcada como paga{quem}.{aviso}")
    return _voltar_para_lista(request)


def gerar_nd(request):
    """Seleciona multas 'a cobrar' de um cliente e emite a ND (docs.md §4.7)."""
    clientes = Cliente.objects.order_by("nome")
    cliente = None
    a_cobrar = []
    cliente_id = request.GET.get("cliente") or request.POST.get("cliente")
    if cliente_id:
        if not id_valido(cliente_id):  # link adulterado dava erro 500 no filtro por pk
            raise Http404("Cliente inválido.")
        cliente = get_object_or_404(Cliente, pk=cliente_id)
        candidatas = cliente.multas.select_related("veiculo").prefetch_related(
            Multa.PREFETCH_REPASSE
        )
        a_cobrar = [m for m in candidatas if m.repasse == "A cobrar"]
    vencimento = _vencimento_nd(cliente, request) if cliente else None
    token = request.POST.get("token", "") if request.method == "POST" else ""
    if request.method == "POST" and cliente:
        marcadas = [m.pk for m in a_cobrar if str(m.pk) in request.POST.getlist("multa")]
        # recarrega sem o prefetch: o serviço reconfere `repasse` no banco, dentro da
        # transação — com os itens em memória, dois envios seguidos gerariam ND em dobro
        selecionadas = list(cliente.multas.select_related("veiculo").filter(pk__in=marcadas))
        try:
            if vencimento["data"] is None:
                raise ValidationError("Vencimento inválido — use uma data a partir de hoje.")
            # mesmo código único da ND manual: o clique duplo não emite duas NDs
            with _envio_unico(request, SESSAO_TOKENS_ND_MULTAS, token) as repetido:
                if repetido:
                    messages.warning(
                        request,
                        "Esta nota de débito já tinha sido emitida — o envio repetido foi "
                        "ignorado e nada foi cobrado em dobro.",
                    )
                    return redirect("financeiro:nds")
                nd = services.gerar_nd_de_multas(
                    cliente, selecionadas, timezone.localdate(), vencimento=vencimento["data"]
                )
            messages.success(request, f"ND {nd.numero:03d} emitida ({brl(nd.total)}).")
            return redirect("financeiro:nds")
        except ValidationError as erro:
            messages.error(request, "; ".join(erro.messages))
    return render(
        request,
        "multas/gerar_nd.html",
        {
            "clientes": clientes,
            "cliente": cliente,
            "multas": a_cobrar,
            "vencimento": vencimento,
            "token": token or uuid4().hex,
        },
    )


#: chave da sessão com os códigos de formulário de "Gerar ND de multas" já usados
SESSAO_TOKENS_ND_MULTAS = "nd_multas_tokens_usados"


def _vencimento_nd(cliente, request):
    """Vencimento da ND de multas (proposta #6, opção A).

    Padrão: o próximo dia de pagamento do cliente — nunca hoje, senão amanhã
    ele vira inadimplente sozinho. A pessoa pode trocar a data no formulário;
    data inválida ou antes de hoje fica em "data" = None (o POST mostra o erro).
    """
    hoje = timezone.localdate()
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
            data = ler_data(digitado)
        except ValidationError:
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
