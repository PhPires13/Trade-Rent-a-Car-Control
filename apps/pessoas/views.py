from datetime import date, timedelta

from django import forms
from django.contrib import messages
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect, render

from apps.alocacoes.models import Alocacao
from apps.financeiro.models import (
    ZERO,
    AplicacaoRecebimento,
    Caucao,
    Cobranca,
    MovimentacaoCaucao,
    MovimentoCredito,
)

from .models import Cliente, CondutorAutorizado

#: Cobranças que ainda pesam no saldo devedor do cliente (docs.md §4.3) —
#: inclui judicial; a fonte única dos conjuntos de status é o modelo.
COBRANCAS_EM_ABERTO = Cobranca.STATUS_DEVIDOS


class ClienteForm(forms.ModelForm):
    class Meta:
        model = Cliente
        fields = [
            "nome",
            "cpf_cnpj",
            "telefone",
            "email",
            "endereco",
            "cnh_numero",
            "cnh_categoria",
            "cnh_validade",
            "dia_vencimento",
            "caucao_referencia",
            "status",
            "observacoes",
        ]
        widgets = {
            "cnh_validade": forms.DateInput(attrs={"type": "date"}),
            "endereco": forms.Textarea(attrs={"rows": 2}),
            "observacoes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # "Inadimplente" é marcado/desmarcado pela rotina diária conforme os atrasos —
        # marcação manual seria revertida à noite (revisão etapa 8). Aqui só Ativo/Inativo.
        choices = [
            (valor, rotulo)
            for valor, rotulo in Cliente.Status.choices
            if valor != Cliente.Status.INADIMPLENTE
        ]
        if self.instance.pk and self.instance.status == Cliente.Status.INADIMPLENTE:
            choices.insert(1, (Cliente.Status.INADIMPLENTE, "Inadimplente (automático)"))
        self.fields["status"].choices = choices
        self.fields["status"].help_text = "Inadimplente é controlado pelos atrasos, todo dia."

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
        fields = ["nome", "cpf", "contato"]


class CondutorForm(forms.ModelForm):
    class Meta:
        model = CondutorAutorizado
        fields = ["nome", "cpf", "cnh_numero", "contato", "observacoes"]
        widgets = {"observacoes": forms.Textarea(attrs={"rows": 2})}


def _telefone_whatsapp(texto):
    """Número pronto para wa.me — prefixa o DDI 55 só quando ainda não veio digitado."""
    digitos = "".join(caractere for caractere in texto if caractere.isdigit())
    if not digitos:
        return ""
    if digitos.startswith("55") and len(digitos) >= 12:
        return digitos
    return f"55{digitos}"


def _saldo_devedor(cliente):
    cobrancas = cliente.cobrancas.filter(status__in=COBRANCAS_EM_ABERTO)
    return sum((cobranca.saldo for cobranca in cobrancas), ZERO)


def _saldo_caucao(cliente):
    caucoes = Caucao.objects.filter(alocacao__cliente=cliente)
    return sum((caucao.saldo for caucao in caucoes), ZERO)


def _alocacao_ativa(cliente):
    return (
        cliente.alocacoes.filter(status=Alocacao.Status.ATIVA)
        .select_related("veiculo")
        .prefetch_related("trocas__veiculo_substituto")
        .first()
    )


def _resumo(cliente, hoje):
    """Bloco de números e alertas usado no detalhe (um cliente só)."""
    validade = cliente.cnh_validade
    return {
        "cliente": cliente,
        "alocacao_ativa": _alocacao_ativa(cliente),
        "saldo_devedor": _saldo_devedor(cliente),
        "credito": MovimentoCredito.saldo_do_cliente(cliente),
        "caucao": _saldo_caucao(cliente),
        "telefone_digitos": _telefone_whatsapp(cliente.telefone),
        "cnh_vencida": bool(validade and validade < hoje),
        "cnh_a_vencer": bool(validade and hoje <= validade <= hoje + timedelta(days=30)),
    }


def _cards_em_lote(clientes, hoje):
    """Monta os cards da lista com queries fixas — nada por cliente (revisão etapa 8).

    O saldo devedor replica Cobranca.saldo em conjunto: valor devido menos
    aplicações de recebimento e descontos de caução das cobranças em aberto.
    """
    ids = [cliente.pk for cliente in clientes]
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
    sinais = {"recebimento": 1, "reforco": 1, "desconto": -1, "devolucao": -1}
    for cliente_id, tipo, total in (
        MovimentacaoCaucao.objects.filter(caucao__alocacao__cliente_id__in=ids)
        .values_list("caucao__alocacao__cliente_id", "tipo")
        .annotate(total=Sum("valor"))
    ):
        caucoes[cliente_id] = caucoes.get(cliente_id, ZERO) + sinais[tipo] * total
    alocacoes = {
        alocacao.cliente_id: alocacao
        for alocacao in Alocacao.objects.filter(
            cliente_id__in=ids, status=Alocacao.Status.ATIVA
        ).select_related("veiculo")
    }

    cards = []
    for cliente in clientes:
        validade = cliente.cnh_validade
        saldo = (
            devido.get(cliente.pk, ZERO)
            - recebido.get(cliente.pk, ZERO)
            - descontado.get(cliente.pk, ZERO)
        )
        cards.append(
            {
                "cliente": cliente,
                "alocacao_ativa": alocacoes.get(cliente.pk),
                "saldo_devedor": saldo,
                "credito": creditos.get(cliente.pk, ZERO),
                "caucao": caucoes.get(cliente.pk, ZERO),
                "telefone_digitos": _telefone_whatsapp(cliente.telefone),
                "cnh_vencida": bool(validade and validade < hoje),
                "cnh_a_vencer": bool(validade and hoje <= validade <= hoje + timedelta(days=30)),
            }
        )
    return cards


def lista(request):
    """Hub de clientes — um card por cliente com carro, dívida e alertas (docs.md §4.1)."""
    status = request.GET.get("status", "")
    busca = request.GET.get("q", "").strip()
    clientes = Cliente.objects.all()
    if status:
        clientes = clientes.filter(status=status)
    else:
        clientes = clientes.exclude(status=Cliente.Status.INATIVO)
    if busca:
        clientes = clientes.filter(nome__icontains=busca)
    hoje = date.today()
    return render(
        request,
        "pessoas/lista.html",
        {
            "cards": _cards_em_lote(list(clientes), hoje),
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
        }
    )
    return render(request, "pessoas/detalhe.html", contexto)


def novo(request):
    form = ClienteForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        cliente = form.save()
        messages.success(request, f"Cliente {cliente.nome} cadastrado.")
        return redirect("pessoas:detalhe", cliente.pk)
    return render(request, "pessoas/cliente_form.html", {"form": form, "cliente": None})


def editar(request, cliente_id):
    cliente = get_object_or_404(Cliente, pk=cliente_id)
    form = ClienteForm(request.POST or None, instance=cliente)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Cadastro de {cliente.nome} atualizado.")
        return redirect("pessoas:detalhe", cliente.pk)
    return render(request, "pessoas/cliente_form.html", {"form": form, "cliente": cliente})


def condutor_novo(request, cliente_id):
    """Adiciona um condutor autorizado pelo formulário curto do detalhe."""
    cliente = get_object_or_404(Cliente, pk=cliente_id)
    if request.method == "POST":
        form = CondutorInlineForm(request.POST)
        if form.is_valid():
            condutor = form.save(commit=False)
            condutor.cliente = cliente
            condutor.save()
            messages.success(request, f"Condutor {condutor.nome} autorizado.")
        else:
            messages.error(request, "Informe ao menos o nome do condutor.")
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
