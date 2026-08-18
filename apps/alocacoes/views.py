from datetime import date

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.html import format_html

from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente

from . import checklist
from .models import Alocacao, TrocaTemporaria, Vistoria
from .services import linha_do_tempo


class AlocacaoForm(forms.ModelForm):
    class Meta:
        model = Alocacao
        fields = [
            "veiculo",
            "cliente",
            "data_inicio",
            "valor_semanal",
            "dia_vencimento",
            "caucao_valor",
            "km_entrega",
            "limite_km",
            "franquia_km_mensal",
            "taxa_km_excedido",
            "observacoes",
        ]
        widgets = {
            "data_inicio": forms.DateInput(attrs={"type": "date"}),
            "observacoes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["veiculo"].queryset = Veiculo.objects.filter(
            status=Veiculo.Status.DISPONIVEL, uso=Veiculo.Uso.LOCACAO
        )
        self.fields["cliente"].queryset = Cliente.objects.exclude(status=Cliente.Status.INATIVO)
        self.fields["dia_vencimento"].required = False

    def clean_dia_vencimento(self):
        valor = self.cleaned_data.get("dia_vencimento")
        return None if valor in (None, "") else valor


class AlocacaoEdicaoForm(forms.ModelForm):
    """Edição do contrato em vigor (docs.md §4.2).

    Veículo, cliente, datas e KM não entram: trocar de carro é troca temporária
    ou encerramento + nova alocação.
    """

    class Meta:
        model = Alocacao
        fields = [
            "valor_semanal",
            "dia_vencimento",
            "caucao_valor",
            "limite_km",
            "franquia_km_mensal",
            "taxa_km_excedido",
            "observacoes",
        ]
        widgets = {"observacoes": forms.Textarea(attrs={"rows": 2})}


class TrocaForm(forms.ModelForm):
    class Meta:
        model = TrocaTemporaria
        fields = [
            "veiculo_substituto",
            "data_retirada",
            "km_retirada",
            "motivo",
            "valor_semanal_ajustado",
            "observacoes",
        ]
        widgets = {
            "data_retirada": forms.DateInput(attrs={"type": "date"}),
            "observacoes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["veiculo_substituto"].queryset = Veiculo.objects.filter(
            status=Veiculo.Status.DISPONIVEL, uso=Veiculo.Uso.LOCACAO
        )


def lista(request):
    mostrar = request.GET.get("mostrar", "ativas")
    alocacoes = Alocacao.objects.select_related("veiculo", "cliente").prefetch_related(
        "trocas__veiculo_substituto"
    )
    if mostrar == "ativas":
        alocacoes = alocacoes.filter(status=Alocacao.Status.ATIVA)
    return render(request, "alocacoes/lista.html", {"alocacoes": alocacoes, "mostrar": mostrar})


def nova(request):
    inicial = {"data_inicio": date.today()}
    veiculo_id = request.GET.get("veiculo")
    if veiculo_id:
        inicial["veiculo"] = veiculo_id
    form = AlocacaoForm(request.POST or None, initial=inicial)
    if request.method == "POST" and form.is_valid():
        alocacao = form.save(commit=False)
        if alocacao.dia_vencimento is None:
            alocacao.dia_vencimento = alocacao.data_inicio.weekday()
        alocacao.save()
        cliente = alocacao.cliente
        if cliente.cnh_validade and cliente.cnh_validade < date.today():
            messages.warning(
                request,
                f"Atenção: a CNH de {cliente.nome} está vencida "
                f"desde {cliente.cnh_validade:%d/%m/%Y}.",
            )
        messages.success(
            request,
            format_html(
                'Veículo {} alocado a {}. <a href="{}" class="font-semibold underline">'
                "Gerar contrato de locação →</a>",
                alocacao.veiculo.placa,
                cliente.nome,
                reverse("alocacoes:contrato", args=[alocacao.pk]),
            ),
        )
        return redirect("alocacoes:lista")
    return render(request, "alocacoes/nova.html", {"form": form})


def editar(request, alocacao_id):
    """Ajusta valores e condições de uma alocação sem mexer no vínculo carro↔cliente."""
    alocacao = get_object_or_404(Alocacao, pk=alocacao_id)
    if alocacao.status != Alocacao.Status.ATIVA:
        # Contrato encerrado é registro histórico — não se altera (revisão etapa 8).
        messages.error(request, "Alocação encerrada não pode ser editada.")
        return redirect("alocacoes:lista")
    form = AlocacaoEdicaoForm(request.POST or None, instance=alocacao)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(
            request,
            f"Alocação de {alocacao.veiculo.placa} → {alocacao.cliente.nome} atualizada.",
        )
        return redirect("alocacoes:lista")
    return render(request, "alocacoes/editar.html", {"alocacao": alocacao, "form": form})


def encerrar(request, alocacao_id):
    alocacao = get_object_or_404(Alocacao, pk=alocacao_id)
    if request.method == "POST":
        try:
            alocacao.encerrar(
                data_termino=date.fromisoformat(request.POST["data_termino"]),
                km_devolucao=int(request.POST["km_devolucao"]),
            )
            messages.success(
                request, f"Alocação encerrada — {alocacao.veiculo.placa} está disponível."
            )
            # Encerrar dispara o acerto de caução quando há saldo retido (docs.md §4.2/§4.4).
            caucao = getattr(alocacao, "caucao", None)
            if caucao and caucao.saldo > 0:
                messages.warning(
                    request,
                    f"Faça o acerto da caução: R$ {caucao.saldo} retidos "
                    f"de {alocacao.cliente.nome}.",
                )
                return redirect("financeiro:caucao_detalhe", caucao.pk)
            return redirect("alocacoes:lista")
        except (ValidationError, KeyError, ValueError) as erro:
            mensagens = getattr(erro, "messages", ["Preencha a data e o KM corretamente."])
            messages.error(request, "; ".join(mensagens))
    return render(request, "alocacoes/encerrar.html", {"alocacao": alocacao, "hoje": date.today()})


def troca_nova(request, alocacao_id):
    alocacao = get_object_or_404(Alocacao, pk=alocacao_id)
    form = TrocaForm(request.POST or None, initial={"data_retirada": date.today()})
    if request.method == "POST" and form.is_valid():
        troca = form.save(commit=False)
        troca.alocacao = alocacao
        try:
            troca.full_clean()
            troca.save()
            messages.success(
                request,
                f"{alocacao.cliente.nome} está com o substituto {troca.veiculo_substituto.placa}.",
            )
            return redirect("alocacoes:lista")
        except ValidationError as erro:
            for mensagens in erro.message_dict.values():
                for mensagem in mensagens:
                    form.add_error(None, mensagem)
    return render(request, "alocacoes/troca_nova.html", {"alocacao": alocacao, "form": form})


def troca_devolver(request, troca_id):
    troca = get_object_or_404(TrocaTemporaria, pk=troca_id)
    if request.method == "POST":
        try:
            troca.devolver(
                data_devolucao=date.fromisoformat(request.POST["data_devolucao"]),
                km_devolucao=int(request.POST["km_devolucao"]),
            )
            messages.success(request, f"Substituto {troca.veiculo_substituto.placa} devolvido.")
            return redirect("alocacoes:lista")
        except (ValidationError, KeyError, ValueError) as erro:
            mensagens = getattr(erro, "messages", ["Preencha a data e o KM corretamente."])
            messages.error(request, "; ".join(mensagens))
    return render(request, "alocacoes/troca_devolver.html", {"troca": troca, "hoje": date.today()})


def timeline(request, veiculo_id):
    veiculo = get_object_or_404(Veiculo, pk=veiculo_id)
    return render(
        request,
        "alocacoes/linha_tempo.html",
        {"veiculo": veiculo, "eventos": linha_do_tempo(veiculo)},
    )


def contrato(request, alocacao_id):
    """Contrato de locação pronto para imprimir/assinar, gerado da alocação.

    É uma minuta com os dados do sistema (partes, veículo, valores, franquia);
    imprimir → PDF pelo navegador. Campos da empresa vêm do settings
    (EMPRESA_*) e ficam em branco para preencher à mão quando não configurados.
    """
    from django.conf import settings

    alocacao = get_object_or_404(
        Alocacao.objects.select_related("veiculo", "cliente"), pk=alocacao_id
    )
    return render(
        request,
        "alocacoes/contrato.html",
        {
            "alocacao": alocacao,
            "veiculo": alocacao.veiculo,
            "cliente": alocacao.cliente,
            "empresa": {
                "razao_social": settings.EMPRESA_RAZAO_SOCIAL,
                "cnpj": settings.EMPRESA_CNPJ,
                "endereco": settings.EMPRESA_ENDERECO,
                "cidade_uf": settings.EMPRESA_CIDADE_UF,
            },
            "hoje": date.today(),
        },
    )


class VistoriaForm(forms.ModelForm):
    class Meta:
        model = Vistoria
        fields = ["tipo", "data", "km", "combustivel", "avarias", "notas", "foto"]
        widgets = {
            "data": forms.DateInput(attrs={"type": "date"}),
            "avarias": forms.Textarea(attrs={"rows": 4}),
            "notas": forms.Textarea(attrs={"rows": 2}),
            "foto": forms.ClearableFileInput(attrs={"accept": "image/*,.pdf"}),
        }


#: Itens impressos no checklist em branco — uma linha por região do carro.
ITENS_CHECKLIST = [
    "Para-choque dianteiro",
    "Para-choque traseiro",
    "Capô",
    "Teto",
    "Porta-malas / tampa",
    "Lateral esquerda (portas e paralamas)",
    "Lateral direita (portas e paralamas)",
    "Retrovisores",
    "Vidros e para-brisa",
    "Faróis e lanternas",
    "Pneus e rodas (4 + estepe)",
    "Bancos e forros",
    "Painel e comandos",
    "Tapetes / interior",
    "Macaco, chave de roda e triângulo",
    "Documento do carro no porta-luvas",
]


def vistoria_imprimir(request, alocacao_id):
    """Checklist de vistoria em branco para imprimir e preencher à mão."""
    alocacao = get_object_or_404(
        Alocacao.objects.select_related("veiculo", "cliente"), pk=alocacao_id
    )
    return render(
        request,
        "alocacoes/vistoria_imprimir.html",
        {
            "alocacao": alocacao,
            "itens": ITENS_CHECKLIST,
            "hoje": date.today(),
            "combustiveis": Vistoria.Combustivel.choices,
        },
    )


def vistoria_nova(request, alocacao_id):
    """Registra a vistoria — a foto do checklist preenchido carrega os campos."""
    alocacao = get_object_or_404(
        Alocacao.objects.select_related("veiculo", "cliente"), pk=alocacao_id
    )
    form = VistoriaForm(request.POST or None, request.FILES or None, initial={"data": date.today()})
    if request.method == "POST" and form.is_valid():
        vistoria = form.save(commit=False)
        vistoria.alocacao = alocacao
        vistoria.save()
        messages.success(
            request,
            f"Vistoria de {vistoria.get_tipo_display().lower()} registrada "
            f"para {alocacao.veiculo.placa}.",
        )
        return redirect("alocacoes:timeline", alocacao.veiculo.pk)
    return render(
        request,
        "alocacoes/vistoria_form.html",
        {
            "alocacao": alocacao,
            "form": form,
            "checklist_leitura": checklist.disponivel(),
            "vistorias": alocacao.vistorias.all(),
        },
    )


def vistoria_extrair(request):
    """Lê a foto do checklist preenchido e devolve os dados para o formulário."""
    if request.method != "POST":
        return JsonResponse({"erro": "Método inválido."}, status=405)
    if not checklist.disponivel():
        return JsonResponse(
            {"erro": "Leitura automática desligada — configure a ANTHROPIC_API_KEY."},
            status=503,
        )
    arquivo = request.FILES.get("foto")
    if not arquivo:
        return JsonResponse({"erro": "Envie a foto do checklist preenchido."}, status=400)
    problema = checklist.validar_upload(arquivo)
    if problema:
        return JsonResponse({"erro": problema}, status=400)
    dados = checklist.extrair_dados([arquivo])
    if dados is None:
        return JsonResponse(
            {"erro": "Não consegui ler o checklist agora — preencha manualmente."},
            status=502,
        )
    if not dados.get("legivel"):
        return JsonResponse(
            {"erro": "A foto não está legível — tire outra com o formulário inteiro."},
            status=422,
        )
    dados.pop("legivel", None)
    return JsonResponse({"dados": dados})
