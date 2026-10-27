from datetime import date, timedelta

from django import forms
from django.contrib import messages
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from apps.alocacoes.models import Alocacao
from apps.busca import nome_casa, numero_casa, parece_numero, sem_acento, so_digitos
from apps.campos import FormularioBR
from apps.financeiro import whatsapp
from apps.financeiro.models import (
    ZERO,
    AplicacaoRecebimento,
    Cobranca,
    MovimentacaoCaucao,
    MovimentoCredito,
    Recebimento,
)
from apps.financeiro.views import _abertas_por_cliente

from . import cnh
from .models import Cliente, CondutorAutorizado

#: Cobranças que ainda pesam no saldo devedor do cliente (docs.md §4.3) —
#: inclui judicial; a fonte única dos conjuntos de status é o modelo.
COBRANCAS_EM_ABERTO = Cobranca.STATUS_DEVIDOS


class ClienteForm(FormularioBR, forms.ModelForm):
    class Meta:
        model = Cliente
        # CNH primeiro: a leitura automática preenche os campos logo abaixo dela
        fields = [
            "cnh_frente",
            "cnh_verso",
            "nome",
            "cpf_cnpj",
            "cnh_numero",
            "cnh_categoria",
            "cnh_validade",
            "telefone",
            "email",
            "endereco",
            "foto",
            "dia_vencimento",
            "caucao_referencia",
            "status",
            "observacoes",
        ]
        widgets = {
            "foto": forms.ClearableFileInput(attrs={"accept": "image/*"}),
            "cnh_frente": forms.ClearableFileInput(attrs={"accept": "image/*,.pdf"}),
            "cnh_verso": forms.ClearableFileInput(attrs={"accept": "image/*,.pdf"}),
            "cnh_validade": forms.DateInput(attrs={"type": "date"}),
            "endereco": forms.Textarea(attrs={"rows": 2}),
            "observacoes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # "Inadimplente" é marcado/desmarcado pela rotina diária conforme os atrasos —
        # marcação manual seria revertida à noite. Aqui só Ativo/Inativo.
        choices = [
            (valor, rotulo)
            for valor, rotulo in Cliente.Status.choices
            if valor != Cliente.Status.INADIMPLENTE
        ]
        if self.instance.pk and self.instance.status == Cliente.Status.INADIMPLENTE:
            choices.insert(1, (Cliente.Status.INADIMPLENTE, "Inadimplente (automático)"))
        self.fields["status"].choices = choices
        self.fields["status"].help_text = "Inadimplente é controlado pelos atrasos, todo dia."
        if not self.instance.pk:
            # cliente novo é sempre ativo — o campo só confundia no cadastro
            del self.fields["status"]
        dia = self.fields["dia_vencimento"]
        dia.choices = [("", "Mesmo dia em que pegar o carro"), *Cliente.DiaSemana.choices]
        self.fields["caucao_referencia"].label = "Caução padrão para novas alocações (R$)"
        self.fields[
            "caucao_referencia"
        ].help_text = "Sugerida ao alocar um carro — o valor que vale é o da alocação."

    def clean_telefone(self):
        """Telefone sem DDD gera link de WhatsApp inválido — só valida ao salvar."""
        telefone = self.cleaned_data["telefone"].strip()
        if telefone and not whatsapp.telefone_whatsapp(telefone):
            raise forms.ValidationError(whatsapp.MENSAGEM_TELEFONE_SEM_DDD)
        return telefone

    def clean_status(self):
        """Inativar com carro na rua esconderia o cliente da lista e da cobrança.

        Espelha a regra inversa da alocação ("cliente inativo não recebe
        veículo"): enquanto houver alocação ativa, o cadastro fica ativo.
        """
        status = self.cleaned_data["status"]
        if (
            status == Cliente.Status.INATIVO
            and self.instance.pk
            and self.instance.alocacoes.filter(status=Alocacao.Status.ATIVA).exists()
        ):
            raise forms.ValidationError(
                "Este cliente ainda está com um carro alugado. "
                "Encerre a alocação antes de marcar como inativo."
            )
        return status


class CondutorInlineForm(forms.ModelForm):
    """Formulário curto do detalhe do cliente — o cliente vem da URL."""

    class Meta:
        model = CondutorAutorizado
        # a CNH também é pedida aqui: quem dirige o carro precisa ter habilitação
        fields = ["nome", "cpf", "cnh_numero", "contato"]
        widgets = {
            "cpf": forms.TextInput(attrs={"inputmode": "numeric"}),
            "cnh_numero": forms.TextInput(attrs={"inputmode": "numeric"}),
            "contato": forms.TextInput(attrs={"inputmode": "tel"}),
        }


def _condutor_repetido(cliente, nome, excluir=None):
    """Mesmo nome (sem diferença de maiúscula/acento) já autorizado para o cliente."""
    alvo = sem_acento(nome.strip())
    condutores = cliente.condutores_autorizados.all()
    if excluir is not None:
        condutores = condutores.exclude(pk=excluir)
    return next((c for c in condutores if sem_acento(c.nome.strip()) == alvo), None)


class CondutorForm(forms.ModelForm):
    class Meta:
        model = CondutorAutorizado
        fields = ["nome", "cpf", "cnh_numero", "contato", "observacoes"]
        widgets = {"observacoes": forms.Textarea(attrs={"rows": 2})}


def _telefone_whatsapp(texto):
    """Número pronto para wa.me (ou '' sem DDD) — mesma regra da cobrança."""
    return whatsapp.telefone_whatsapp(texto)


#: Sinal de cada tipo de movimentação no saldo retido da caução (espelha Caucao.saldo).
SINAL_MOVIMENTACAO_CAUCAO = {
    MovimentacaoCaucao.Tipo.RECEBIMENTO: 1,
    MovimentacaoCaucao.Tipo.REFORCO: 1,
    MovimentacaoCaucao.Tipo.DESCONTO: -1,
    MovimentacaoCaucao.Tipo.DEVOLUCAO: -1,
}


def _saldos_em_lote(ids):
    """Saldo devedor, crédito e caução de vários clientes com queries fixas.

    Fonte única dos números da lista e da ficha — nada é consultado por
    cliente ou por cobrança. O saldo devedor replica Cobranca.saldo em
    conjunto: valor devido menos aplicações de recebimento e descontos de
    caução das cobranças em aberto. Devolve {cliente_id: {...}} para todos os ids.
    """
    devido = dict(
        Cobranca.objects.filter(cliente_id__in=ids, status__in=COBRANCAS_EM_ABERTO)
        .values_list("cliente_id")
        .annotate(total=Sum("valor"))
    )
    recebido = dict(
        AplicacaoRecebimento.objects.filter(
            cobranca__cliente_id__in=ids, cobranca__status__in=COBRANCAS_EM_ABERTO
        )
        .values_list("cobranca__cliente_id")
        .annotate(total=Sum("valor"))
    )
    descontado = dict(
        MovimentacaoCaucao.objects.filter(
            cobranca__cliente_id__in=ids, cobranca__status__in=COBRANCAS_EM_ABERTO
        )
        .values_list("cobranca__cliente_id")
        .annotate(total=Sum("valor"))
    )
    creditos = {}
    for cliente_id, tipo, total in (
        MovimentoCredito.objects.filter(cliente_id__in=ids)
        .values_list("cliente_id", "tipo")
        .annotate(total=Sum("valor"))
    ):
        sinal = 1 if tipo == MovimentoCredito.Tipo.ENTRADA else -1
        creditos[cliente_id] = creditos.get(cliente_id, ZERO) + sinal * total
    caucoes = {}
    for cliente_id, tipo, total in (
        MovimentacaoCaucao.objects.filter(caucao__alocacao__cliente_id__in=ids)
        .values_list("caucao__alocacao__cliente_id", "tipo")
        .annotate(total=Sum("valor"))
    ):
        caucoes[cliente_id] = caucoes.get(cliente_id, ZERO) + (
            SINAL_MOVIMENTACAO_CAUCAO[tipo] * total
        )
    return {
        cliente_id: {
            "saldo_devedor": devido.get(cliente_id, ZERO)
            - recebido.get(cliente_id, ZERO)
            - descontado.get(cliente_id, ZERO),
            "credito": creditos.get(cliente_id, ZERO),
            "caucao": caucoes.get(cliente_id, ZERO),
        }
        for cliente_id in ids
    }


def _card(cliente, hoje, saldos, alocacao_ativa):
    """Números e alertas de um cliente — mesmo formato na lista e na ficha."""
    validade = cliente.cnh_validade
    return {
        "cliente": cliente,
        "alocacao_ativa": alocacao_ativa,
        **saldos,
        "telefone_digitos": _telefone_whatsapp(cliente.telefone),
        "cnh_vencida": bool(validade and validade < hoje),
        "cnh_a_vencer": bool(validade and hoje <= validade <= hoje + timedelta(days=30)),
    }


def _resumo(cliente, hoje):
    """Bloco de números e alertas da ficha (um cliente só)."""
    # cliente com dois carros ao mesmo tempo vê os dois na ficha
    ativas = list(
        cliente.alocacoes.filter(status=Alocacao.Status.ATIVA)
        .select_related("veiculo")
        .prefetch_related("trocas__veiculo_substituto")  # aviso de carro substituto
        .order_by("-data_inicio", "-pk")
    )
    card = _card(
        cliente, hoje, _saldos_em_lote([cliente.pk])[cliente.pk], ativas[0] if ativas else None
    )
    card["alocacoes_ativas"] = ativas
    if ativas:
        from apps.financeiro.models import Caucao  # import local evita ciclo de import
        from apps.manutencao.services import oficina_por_veiculo

        # carro na oficina e caução da alocação (depositada ou não) no cartão do carro
        oficina = oficina_por_veiculo([a.veiculo_id for a in ativas])
        caucoes = {
            c.alocacao_id: c
            for c in Caucao.com_saldos_anotados(Caucao.objects.filter(alocacao__in=ativas))
        }
        for alocacao in ativas:
            alocacao.na_oficina = oficina.get(alocacao.veiculo_id)
            caucao = caucoes.get(alocacao.pk)
            alocacao.caucao_recebida = caucao.recebido if caucao else ZERO
    return card


def _cards_em_lote(clientes, hoje):
    """Cards da lista com queries fixas — nada por cliente."""
    ids = [cliente.pk for cliente in clientes]
    saldos = _saldos_em_lote(ids)
    # "R$ X vencido" com a mesma conta do Quem deve; o resto é "a vencer"
    from apps.alocacoes.views import dividas_por_cliente  # import local evita ciclo

    dividas = dividas_por_cliente(ids)
    for cliente_id, saldo in saldos.items():
        divida = dividas.get(cliente_id) or {}
        saldo["vencido"] = divida.get("ate_hoje", ZERO)
        saldo["a_vencer"] = divida.get("a_vencer", ZERO)
    alocacoes = {
        alocacao.cliente_id: alocacao
        for alocacao in Alocacao.objects.filter(
            cliente_id__in=ids, status=Alocacao.Status.ATIVA
        ).select_related("veiculo")
    }
    return [
        _card(cliente, hoje, saldos[cliente.pk], alocacoes.get(cliente.pk)) for cliente in clientes
    ]


def lista(request):
    """Hub de clientes — um card por cliente com carro, dívida e alertas (docs.md §4.1)."""
    status = request.GET.get("status", "")
    busca = request.GET.get("q", "").strip()
    clientes = Cliente.objects.all()
    if status:
        clientes = clientes.filter(status=status)
    else:
        clientes = clientes.exclude(status=Cliente.Status.INATIVO)
    clientes = list(clientes)
    if busca:
        # sem acento/maiúscula; números servem para CPF/CNPJ e telefone
        texto = sem_acento(busca)
        digitos = so_digitos(busca) if parece_numero(busca) else ""
        clientes = [
            c
            for c in clientes
            if nome_casa(texto, c.nome)
            or (digitos and (numero_casa(digitos, c.cpf_cnpj) or numero_casa(digitos, c.telefone)))
        ]
    hoje = date.today()
    return render(
        request,
        "pessoas/lista.html",
        {
            "cards": _cards_em_lote(clientes, hoje),
            "status": status,
            "busca": busca,
            "statuses": Cliente.Status.choices,
        },
    )


def detalhe(request, cliente_id):
    """Ficha completa do cliente — cadastro, financeiro, carros, multas e condutores."""
    cliente = get_object_or_404(Cliente, pk=cliente_id)
    hoje = date.today()
    historico = (
        cliente.alocacoes.exclude(status=Alocacao.Status.ATIVA)
        .select_related("veiculo")
        .order_by("-data_inicio")
    )
    multas = cliente.multas.select_related("veiculo").order_by("-data_infracao")
    contexto = _resumo(cliente, hoje)
    contexto.update(
        {
            "historico": historico,
            "total_multas": multas.count(),
            "ultimas_multas": multas[:5],
            "condutores": cliente.condutores_autorizados.all(),
            "form_condutor": CondutorInlineForm(),
            # bloco "Em aberto": último pagamento e a mensagem única de cobrança
            "ultimo_pagamento": cliente.recebimentos.exclude(
                forma=Recebimento.Forma.CREDITO
            ).first(),
            "link_cobrar": whatsapp.link_cliente(
                cliente, _abertas_por_cliente([cliente.pk]).get(cliente.pk, []), hoje
            )
            if contexto["saldo_devedor"] > 0
            else "",
        }
    )
    return render(request, "pessoas/detalhe.html", contexto)


def novo(request):
    form = ClienteForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        cliente = form.save()
        messages.success(request, f"Cliente {cliente.nome} cadastrado.")
        return redirect("pessoas:detalhe", cliente.pk)
    return render(
        request,
        "pessoas/cliente_form.html",
        {"form": form, "cliente": None, "cnh_leitura": cnh.disponivel()},
    )


def editar(request, cliente_id):
    cliente = get_object_or_404(Cliente, pk=cliente_id)
    form = ClienteForm(request.POST or None, request.FILES or None, instance=cliente)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Cadastro de {cliente.nome} atualizado.")
        return redirect("pessoas:detalhe", cliente.pk)
    return render(
        request,
        "pessoas/cliente_form.html",
        {"form": form, "cliente": cliente, "cnh_leitura": cnh.disponivel()},
    )


def condutor_novo(request, cliente_id):
    """Adiciona um condutor autorizado pelo formulário curto do detalhe."""
    cliente = get_object_or_404(Cliente, pk=cliente_id)
    if request.method == "POST":
        form = CondutorInlineForm(request.POST)
        repetido = form.is_valid() and _condutor_repetido(cliente, form.cleaned_data["nome"])
        if repetido:
            # reenvio do formulário (ou cadastro em dobro): não cria outro igual
            messages.warning(
                request, f"{repetido.nome} já é condutor(a) autorizado(a) deste cliente."
            )
        elif form.is_valid():
            condutor = form.save(commit=False)
            condutor.cliente = cliente
            condutor.save()
            messages.success(request, f"Condutor {condutor.nome} autorizado.")
        elif "nome" in form.errors:
            messages.error(request, "Informe ao menos o nome do condutor.")
        else:
            # com a CNH no formulário curto, o erro pode ser de outro campo
            for campo, erros in form.errors.items():
                rotulo = form.fields[campo].label if campo in form.fields else ""
                messages.error(
                    request, f"{rotulo}: {' '.join(erros)}" if rotulo else " ".join(erros)
                )
    return redirect("pessoas:detalhe", cliente.pk)


def condutor_editar(request, condutor_id):
    condutor = get_object_or_404(CondutorAutorizado, pk=condutor_id)
    form = CondutorForm(request.POST or None, instance=condutor)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Condutor {condutor.nome} atualizado.")
        if condutor.cliente_id:
            return redirect("pessoas:detalhe", condutor.cliente_id)
        return redirect("pessoas:lista")
    return render(request, "pessoas/condutor_form.html", {"form": form, "condutor": condutor})


def condutor_remover(request, condutor_id):
    """Tira a autorização do condutor (botão com confirmação na ficha do cliente).

    Condutor que já aparece em multa não é apagado: a multa precisa continuar
    dizendo quem dirigia. Nesse caso a tela explica e nada muda.
    """
    condutor = get_object_or_404(CondutorAutorizado, pk=condutor_id)
    destino = (
        redirect("pessoas:detalhe", condutor.cliente_id)
        if condutor.cliente_id
        else redirect("pessoas:lista")
    )
    if request.method != "POST":
        return destino
    if condutor.multas.exists():
        messages.error(
            request,
            f"{condutor.nome} aparece em multa(s) como quem dirigia, por isso não pode ser "
            "removido. Se ele não está mais autorizado, anote isso em “editar” > observações.",
        )
        return destino
    nome = condutor.nome
    condutor.delete()
    messages.success(request, f"{nome} não é mais condutor autorizado.")
    return destino


def cnh_extrair(request):
    """Lê as fotos da CNH e devolve os dados como sugestão para o formulário.

    Nada é gravado aqui — o retorno preenche os campos na tela e quem cadastra
    valida e ajusta antes de salvar (pedido do dono: preencher para validação).
    """
    if request.method != "POST":
        return JsonResponse({"erro": "Método inválido."}, status=405)
    if not cnh.disponivel():
        return JsonResponse(
            {"erro": "Leitura automática desligada — configure a ANTHROPIC_API_KEY."},
            status=503,
        )
    fotos = [f for f in [request.FILES.get("cnh_frente"), request.FILES.get("cnh_verso")] if f]
    if not fotos:
        return JsonResponse({"erro": "Envie a foto da frente e/ou do verso da CNH."}, status=400)
    for foto in fotos:
        problema = cnh.validar_upload(foto)
        if problema:
            return JsonResponse({"erro": problema}, status=400)
    dados = cnh.extrair_dados(fotos)
    if dados is None:
        return JsonResponse(
            {"erro": "Não consegui ler a CNH agora — preencha manualmente ou tente de novo."},
            status=502,
        )
    if not dados.get("legivel"):
        return JsonResponse(
            {"erro": "As fotos não estão legíveis — tire outra foto com o documento inteiro."},
            status=422,
        )
    dados.pop("legivel", None)
    return JsonResponse({"dados": dados})
