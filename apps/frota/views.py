from datetime import date
from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.manutencao.services import contagem_alertas_por_veiculo
from apps.multas.models import Multa

from . import crlv, desmobilizacao
from .models import Categoria, Fornecedor, Veiculo, normalizar_placa


def _quem_esta_com_o_carro(veiculo):
    """Motorista atual do veículo — alocação ativa ou empréstimo como substituto."""
    alocacao = (
        veiculo.alocacoes.filter(status=Alocacao.Status.ATIVA).select_related("cliente").first()
    )
    if alocacao:
        return {
            "motorista": alocacao.cliente.nome,
            "valor_semanal": alocacao.valor_semanal,
            "alocacao": alocacao,
        }
    troca = (
        veiculo.trocas_como_substituto.filter(data_devolucao__isnull=True)
        .select_related("alocacao__cliente")
        .first()
    )
    if troca:
        return {
            "motorista": f"{troca.alocacao.cliente.nome} (substituto)",
            "valor_semanal": troca.valor_semanal_ajustado or troca.alocacao.valor_semanal,
            "alocacao": troca.alocacao,
        }
    return {"motorista": None, "valor_semanal": None, "alocacao": None}


def hub(request):
    """Hub da frota — cards de veículos com filtros de status, uso e placa (docs.md §4.1).

    As informações dos cards vêm em queries fixas (lote), não por veículo —
    a frota cresce e esta é uma página de navegação principal.
    """
    status = request.GET.get("status", "")
    uso = request.GET.get("uso", "")
    placa = request.GET.get("placa", "").strip()

    veiculos = Veiculo.objects.select_related("categoria").order_by("placa")
    if status and status != "todos":
        veiculos = veiculos.filter(status=status)
    elif not status:
        # Padrão: vendidos ficam escondidos até serem pedidos no filtro.
        veiculos = veiculos.exclude(status=Veiculo.Status.VENDIDO)
    if uso and uso != "todos":
        veiculos = veiculos.filter(uso=uso)
    if placa:
        veiculos = veiculos.filter(placa__icontains=normalizar_placa(placa))

    veiculos = list(veiculos)
    alocacoes_ativas = {
        alocacao.veiculo_id: alocacao
        for alocacao in Alocacao.objects.filter(status=Alocacao.Status.ATIVA).select_related(
            "cliente"
        )
    }
    trocas_ativas = {
        troca.veiculo_substituto_id: troca
        for troca in TrocaTemporaria.objects.filter(data_devolucao__isnull=True).select_related(
            "alocacao__cliente"
        )
    }
    fici_pendentes = dict(
        Multa.objects.filter(fici_status=Multa.Fici.PENDENTE)
        .values_list("veiculo_id")
        .annotate(total=Count("id"))
    )
    preventivas = contagem_alertas_por_veiculo(veiculos)

    cards = []
    for veiculo in veiculos:
        alocacao = alocacoes_ativas.get(veiculo.pk)
        troca = trocas_ativas.get(veiculo.pk)
        if alocacao:
            motorista, valor = alocacao.cliente.nome, alocacao.valor_semanal
        elif troca:
            motorista = f"{troca.alocacao.cliente.nome} (substituto)"
            valor = troca.valor_semanal_ajustado or troca.alocacao.valor_semanal
        else:
            motorista = valor = None
        cards.append(
            {
                "veiculo": veiculo,
                "motorista": motorista,
                "valor_semanal": valor,
                "preventivas_alerta": preventivas.get(veiculo.pk, 0),
                "multas_fici_pendentes": fici_pendentes.get(veiculo.pk, 0),
            }
        )

    return render(
        request,
        "frota/hub.html",
        {
            "cards": cards,
            "status_escolhido": status,
            "uso_escolhido": uso,
            "placa_buscada": placa,
            "opcoes_status": Veiculo.Status.choices,
            "opcoes_uso": Veiculo.Uso.choices,
        },
    )


def detalhe(request, veiculo_id):
    """Página-hub de um veículo — cadastro, situação atual e atalhos (docs.md §4.1)."""
    veiculo = get_object_or_404(Veiculo.objects.select_related("categoria"), pk=veiculo_id)
    contexto = {
        "veiculo": veiculo,
        "total_alocacoes": veiculo.alocacoes.count(),
        "total_km": veiculo.registros_km.count(),
        "total_manutencoes": veiculo.manutencoes.count(),
        "total_multas": veiculo.multas.count(),
        "total_sinistros": veiculo.sinistros.count(),
    }
    contexto.update(_quem_esta_com_o_carro(veiculo))
    return render(request, "frota/detalhe.html", contexto)


class VeiculoForm(forms.ModelForm):
    """Cadastro do veículo — os campos de venda são geridos pelo fluxo "Vender"."""

    SECOES = (
        (
            "Identificação",
            [
                "placa",
                "renavam",
                "chassi",
                "marca_modelo",
                "ano",
                "categoria",
                "uso",
                "foto",
                "documento",
            ],
        ),
        (
            "IPVA e licenciamento",
            [
                "ipva_ano",
                "ipva_valor",
                "ipva_vencimento",
                "ipva_pago_em",
                "licenciamento_vencimento",
            ],
        ),
        (
            "Aquisição",
            [
                "data_aquisicao",
                "valor_compra",
                "custos_entrada",
                "km_compra",
                "km_atual",
                "valor_venda_estimado",
            ],
        ),
        (
            "Proteção e vigências",
            [
                "mensalidade_protecao",
                "chave_reserva",
                "rastreador_fornecedor",
                "rastreador_vigencia_fim",
                "bateria_data_troca",
                "bateria_fornecedor",
                "bateria_garantia_fim",
            ],
        ),
        ("Outros", ["observacoes"]),
    )

    class Meta:
        model = Veiculo
        # "status" fica de fora: é gerido pelos fluxos (alocar/encerrar/manutenção/vender);
        # editar aqui dessincronizaria a alocação ativa e a venda (revisão etapa 8).
        fields = [
            "placa",
            "renavam",
            "chassi",
            "marca_modelo",
            "ano",
            "categoria",
            "uso",
            "foto",
            "documento",
            "ipva_ano",
            "ipva_valor",
            "ipva_vencimento",
            "ipva_pago_em",
            "licenciamento_vencimento",
            "data_aquisicao",
            "valor_compra",
            "custos_entrada",
            "km_compra",
            "km_atual",
            "valor_venda_estimado",
            "mensalidade_protecao",
            "chave_reserva",
            "rastreador_fornecedor",
            "rastreador_vigencia_fim",
            "bateria_data_troca",
            "bateria_fornecedor",
            "bateria_garantia_fim",
            "observacoes",
        ]
        widgets = {
            "foto": forms.ClearableFileInput(attrs={"accept": "image/*"}),
            "documento": forms.ClearableFileInput(attrs={"accept": "image/*,.pdf"}),
            "data_aquisicao": forms.DateInput(attrs={"type": "date"}),
            "ipva_vencimento": forms.DateInput(attrs={"type": "date"}),
            "ipva_pago_em": forms.DateInput(attrs={"type": "date"}),
            "licenciamento_vencimento": forms.DateInput(attrs={"type": "date"}),
            "rastreador_vigencia_fim": forms.DateInput(attrs={"type": "date"}),
            "bateria_data_troca": forms.DateInput(attrs={"type": "date"}),
            "bateria_garantia_fim": forms.DateInput(attrs={"type": "date"}),
            "observacoes": forms.Textarea(attrs={"rows": 3}),
        }

    def clean_placa(self):
        """Normaliza antes da checagem de duplicidade — o modelo grava sem hífen e em maiúsculas."""
        return normalizar_placa(self.cleaned_data["placa"])

    def clean_km_atual(self):
        """O odômetro nunca anda para trás — todos os fluxos só aumentam o KM.

        Reduzir aqui esconderia preventivas vencidas; correção real de odômetro
        é exceção e fica no Admin.
        """
        km = self.cleaned_data["km_atual"]
        if self.instance.pk and km < self.instance.km_atual:
            raise ValidationError(
                f"KM atual ({km}) menor que o registrado ({self.instance.km_atual}). "
                "O odômetro não diminui — correções excepcionais são feitas no Admin."
            )
        return km

    @property
    def secoes(self):
        return [(titulo, [self[nome] for nome in nomes]) for titulo, nomes in self.SECOES]


def _formulario_veiculo(request, veiculo):
    form = VeiculoForm(request.POST or None, request.FILES or None, instance=veiculo)
    if request.method == "POST" and form.is_valid():
        salvo = form.save()
        messages.success(request, f"Veículo {salvo.placa} salvo.")
        return redirect("frota:detalhe", salvo.pk)
    return render(
        request,
        "frota/veiculo_form.html",
        {"form": form, "veiculo": veiculo, "crlv_leitura": crlv.disponivel()},
    )


def veiculo_novo(request):
    return _formulario_veiculo(request, None)


def veiculo_editar(request, veiculo_id):
    return _formulario_veiculo(request, get_object_or_404(Veiculo, pk=veiculo_id))


class CategoriaForm(forms.ModelForm):
    class Meta:
        model = Categoria
        fields = ["nome", "valor_semanal_referencia", "observacoes"]
        widgets = {"observacoes": forms.Textarea(attrs={"rows": 2})}


class FornecedorForm(forms.ModelForm):
    class Meta:
        model = Fornecedor
        fields = ["nome", "cnpj", "contato", "tipo_servico", "observacoes"]
        widgets = {"observacoes": forms.Textarea(attrs={"rows": 2})}


def _cadastro_simples(request, form_classe, modelo, template, chave_id, contexto_extra):
    """Cria e edita registros de apoio na mesma tela (categorias e fornecedores)."""
    form = form_classe()
    if request.method == "POST":
        registro_id = (request.POST.get(chave_id) or "").strip() or None
        if registro_id is not None and not registro_id.isdigit():
            raise Http404("Registro inválido.")
        instancia = get_object_or_404(modelo, pk=registro_id) if registro_id else None
        form = form_classe(request.POST, instance=instancia)
        if form.is_valid():
            salvo = form.save()
            messages.success(request, f"{salvo.nome} salvo.")
            return redirect(request.path)
        messages.error(
            request,
            "; ".join(f"{campo}: {erros[0]}" for campo, erros in form.errors.items()),
        )
    return render(request, template, {"form": form, **contexto_extra()})


def categorias(request):
    """Categorias de veículo e seu valor semanal de referência (docs.md §4.1)."""
    return _cadastro_simples(
        request,
        CategoriaForm,
        Categoria,
        "frota/categorias.html",
        "categoria_id",
        lambda: {
            "categorias": Categoria.objects.annotate(total_veiculos=Count("veiculos")).order_by(
                "nome"
            )
        },
    )


def fornecedores(request):
    """Oficinas e prestadores de serviço (docs.md §4.1)."""
    return _cadastro_simples(
        request,
        FornecedorForm,
        Fornecedor,
        "frota/fornecedores.html",
        "fornecedor_id",
        lambda: {"fornecedores": Fornecedor.objects.order_by("nome")},
    )


def ranking(request):
    """Ranking de desmobilização — candidatos à venda primeiro (docs.md §4.9)."""
    fichas, media = desmobilizacao.ranking_da_frota()
    vendidos = Veiculo.objects.filter(status=Veiculo.Status.VENDIDO).order_by("-data_venda")
    fichas_vendidos = desmobilizacao.montar_fichas_em_lote(vendidos)
    return render(
        request,
        "frota/ranking.html",
        {"fichas": fichas, "media": media, "fichas_vendidos": fichas_vendidos},
    )


def ficha(request, veiculo_id):
    veiculo = get_object_or_404(Veiculo, pk=veiculo_id)
    # Só a média da frota entra na comparação: montar o ranking inteiro para
    # exibir um carro custava ~200 queries (revisão de performance).
    media = desmobilizacao.media_custo_km_frota()
    ficha = desmobilizacao.avaliar(desmobilizacao.montar_ficha(veiculo), media)
    return render(request, "frota/ficha.html", {"f": ficha, "veiculo": veiculo})


def vender(request, veiculo_id):
    veiculo = get_object_or_404(Veiculo, pk=veiculo_id)
    if request.method == "POST":
        try:
            desmobilizacao.registrar_venda(
                veiculo,
                data=date.fromisoformat(request.POST["data"]),
                valor=Decimal(request.POST["valor"].replace(",", ".")),
                comprador=request.POST.get("comprador", ""),
                custos=(
                    Decimal(request.POST["custos"].replace(",", "."))
                    if request.POST.get("custos", "").strip()
                    else None
                ),
                km=int(request.POST["km"]) if request.POST.get("km", "").strip() else None,
            )
            messages.success(
                request,
                f"{veiculo.placa} vendido — crédito da venda fica fora da base do DAS. "
                "Resultado final na ficha do veículo.",
            )
            return redirect("frota:ficha", veiculo.pk)
        except (ValidationError, KeyError, InvalidOperation, ValueError) as erro:
            mensagens = getattr(erro, "messages", ["Preencha os campos corretamente."])
            messages.error(request, "; ".join(mensagens))
    return render(request, "frota/vender.html", {"veiculo": veiculo, "hoje": date.today()})


def crlv_extrair(request):
    """Lê a foto/PDF do CRLV e devolve os dados como sugestão para o formulário.

    Nada é gravado aqui — o retorno preenche placa/renavam/chassi/modelo/ano
    na tela e quem cadastra valida e ajusta antes de salvar.
    """
    if request.method != "POST":
        return JsonResponse({"erro": "Método inválido."}, status=405)
    if not crlv.disponivel():
        return JsonResponse(
            {"erro": "Leitura automática desligada — configure a ANTHROPIC_API_KEY."},
            status=503,
        )
    arquivo = request.FILES.get("documento")
    if not arquivo:
        return JsonResponse({"erro": "Envie a foto ou o PDF do CRLV."}, status=400)
    problema = crlv.validar_upload(arquivo)
    if problema:
        return JsonResponse({"erro": problema}, status=400)
    dados = crlv.extrair_dados([arquivo])
    if dados is None:
        return JsonResponse(
            {"erro": "Não consegui ler o documento agora — preencha manualmente ou tente de novo."},
            status=502,
        )
    if not dados.get("legivel"):
        return JsonResponse(
            {"erro": "O documento não está legível — envie outra foto com ele inteiro."},
            status=422,
        )
    dados.pop("legivel", None)
    return JsonResponse({"dados": dados})
