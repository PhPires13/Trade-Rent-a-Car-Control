from decimal import InvalidOperation

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db.models import Count, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.campos import FormularioBR, dinheiro_br, ler_data
from apps.formatos import milhar, numero_br
from apps.manutencao.services import contagem_alertas_por_veiculo
from apps.multas.models import Multa

from . import alertas, crlv, desmobilizacao
from .models import Categoria, Fornecedor, PagamentoIpva, Veiculo, normalizar_placa


def _motorista_e_valor(alocacao, troca):
    """(motorista, valor semanal) de quem roda com o carro — mesma regra no hub e no detalhe.

    `alocacao` é a alocação ativa do próprio carro; `troca`, a troca em aberto em
    que ele é o substituto emprestado. Sem nenhuma das duas: (None, None).
    """
    if alocacao:
        return alocacao.cliente.nome, alocacao.valor_semanal
    if troca:
        return (
            f"{troca.alocacao.cliente.nome} (substituto)",
            troca.valor_semanal_ajustado or troca.alocacao.valor_semanal,
        )
    return None, None


def _quem_esta_com_o_carro(veiculo, na_oficina=alertas._BUSCAR):
    """Motorista atual do veículo — alocação ativa ou empréstimo como substituto."""
    alocacao = (
        veiculo.alocacoes.filter(status=Alocacao.Status.ATIVA).select_related("cliente").first()
    )
    troca = None
    if not alocacao:
        troca = (
            veiculo.trocas_como_substituto.filter(data_devolucao__isnull=True)
            .select_related("alocacao__cliente")
            .first()
        )
    motorista, valor = _motorista_e_valor(alocacao, troca)
    return {
        "motorista": motorista,
        "valor_semanal": valor,
        # o dono do contrato pode estar rodando com um substituto (carro na oficina)
        "substituto_em_uso": alocacao.troca_ativa if alocacao else None,
        "impedimento_venda": _impedimento_venda(alocacao, troca, veiculo, na_oficina),
    }


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
    # mesmo filtro da faixa de pendências (alertas.py): multa anulada não pede FICI
    fici_pendentes = dict(
        Multa.objects.fici_pendente().values_list("veiculo_id").annotate(total=Count("id"))
    )
    preventivas = contagem_alertas_por_veiculo(veiculos)

    cards = []
    for veiculo in veiculos:
        motorista, valor = _motorista_e_valor(
            alocacoes_ativas.get(veiculo.pk), trocas_ativas.get(veiculo.pk)
        )
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
    veiculo = get_object_or_404(
        Veiculo.objects.select_related("categoria").prefetch_related("pagamentos_ipva"),
        pk=veiculo_id,
    )
    contexto = {
        "veiculo": veiculo,
        "total_alocacoes": veiculo.alocacoes.count(),
        "total_km": veiculo.registros_km.count(),
        "total_manutencoes": veiculo.manutencoes.count(),
        "total_multas": veiculo.multas.count(),
        "total_sinistros": veiculo.sinistros.count(),
    }
    # uma consulta só para o selo, a faixa de pendências e o bloqueio da venda
    contexto["em_oficina"] = em_oficina = desmobilizacao.oficina_impede_venda(veiculo)
    contexto.update(_quem_esta_com_o_carro(veiculo, em_oficina))
    contexto["pendencias"] = alertas.pendencias_do_veiculo(veiculo, na_oficina=em_oficina)
    # IPVA do ano mais recente lançado (todas as parcelas dele), do prefetch
    ipvas = list(veiculo.pagamentos_ipva.all())
    ultimo_ano = max((p.ano for p in ipvas), default=None)
    contexto["ipva_recente"] = sorted(
        (p for p in ipvas if p.ano == ultimo_ano), key=lambda p: p.parcela
    )
    return render(request, "frota/detalhe.html", contexto)


def _impedimento_venda(alocacao, troca, veiculo=None, na_oficina=alertas._BUSCAR):
    """Por que o carro ainda não pode ser vendido — a regra de registrar_venda, explicada antes.

    Recebe o que _quem_esta_com_o_carro já buscou (sem consultas extras): a
    alocação ativa do carro ou a troca em aberto em que ele é o substituto — a
    troca do próprio carro na oficina só existe com a alocação dele ativa.
    Com `veiculo`, também barra o carro ainda na oficina (manutenção aberta).
    Devolve {"texto", "url", "acao"} ou None quando a venda está liberada.
    """
    if alocacao:
        return {
            "texto": f"Este carro está com {alocacao.cliente.nome} desde "
            f"{alocacao.data_inicio:%d/%m/%Y}. Encerre a alocação antes de vender.",
            "url": reverse("alocacoes:encerrar", args=[alocacao.pk]),
            "acao": "Encerrar a alocação",
        }
    if troca:
        return {
            "texto": f"Este carro está emprestado a {troca.alocacao.cliente.nome} (troca) desde "
            f"{troca.data_retirada:%d/%m/%Y}. Registre a devolução antes de vender.",
            "url": reverse("alocacoes:troca_devolver", args=[troca.pk]),
            "acao": "Registrar a devolução",
        }
    if veiculo is not None:
        if na_oficina is alertas._BUSCAR:
            na_oficina = desmobilizacao.oficina_impede_venda(veiculo)
        if na_oficina:
            volta = reverse("frota:vender", args=[veiculo.pk])
            return {
                "texto": desmobilizacao.texto_oficina_impede_venda(na_oficina),
                "url": f"{reverse('manutencao:voltou', args=[na_oficina.pk])}?volta={volta}",
                "acao": "Carro voltou da oficina",
            }
    return None


#: Seções da tela de cadastro, na ordem em que aparecem — também definem os campos
#: do VeiculoForm (fonte única: campo fora daqui não é exibido nem salvo).
SECOES_VEICULO = (
    (
        "Identificação",
        [
            "documento",
            "placa",
            "renavam",
            "chassi",
            "marca_modelo",
            "ano",
            "categoria",
            "uso",
            "foto",
        ],
    ),
    # o IPVA (ano a ano, com parcelas) é lançado na tela "IPVA" do carro
    ("Licenciamento", ["licenciamento_vencimento"]),
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

_CAMPOS_DE_DATA_VEICULO = (
    "data_aquisicao",
    "licenciamento_vencimento",
    "rastreador_vigencia_fim",
    "bateria_data_troca",
    "bateria_garantia_fim",
)


class VeiculoForm(FormularioBR, forms.ModelForm):
    """Cadastro do veículo — os campos de venda são geridos pelo fluxo "Vender"."""

    class Meta:
        model = Veiculo
        # "status" fica de fora: é gerido pelos fluxos (alocar/encerrar/manutenção/vender);
        # editar aqui dessincronizaria a alocação ativa e a venda.
        fields = [nome for _, nomes in SECOES_VEICULO for nome in nomes]
        widgets = {
            "foto": forms.ClearableFileInput(attrs={"accept": "image/*"}),
            "documento": forms.ClearableFileInput(attrs={"accept": "image/*,.pdf"}),
            "observacoes": forms.Textarea(attrs={"rows": 3}),
            **{nome: forms.DateInput(attrs={"type": "date"}) for nome in _CAMPOS_DE_DATA_VEICULO},
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and self.instance.uso == Veiculo.Uso.LOCACAO:
            # só é exigida quando o carro com lançamentos passa a "Fora de locação"
            self.fields["confirmar_fora_locacao"] = forms.BooleanField(
                label="Os gastos já lançados deste carro vão sair dos relatórios dos meses "
                "passados — confirmo",
                required=False,
            )
        if not self.instance.pk:
            # carro novo: em branco, o KM atual é o da compra (preenchido no clean)
            self.fields["km_atual"].required = False
            self.fields["km_atual"].help_text = "Deixe em branco se for o mesmo KM da compra"

    @property
    def pede_confirmacao_uso(self):
        return "confirmar_fora_locacao" in self.fields and (
            "confirmar_fora_locacao" in self.errors or bool(self.data.get("confirmar_fora_locacao"))
        )

    def clean_placa(self):
        """Normaliza antes da checagem de duplicidade — o modelo grava sem hífen e em maiúsculas."""
        return normalizar_placa(self.cleaned_data["placa"])

    def clean_km_atual(self):
        """O odômetro nunca anda para trás pela edição (ETAPAS, etapa 8).

        Reduzir aqui esconderia preventivas vencidas. Número digitado errado se
        corrige no lançamento de onde ele veio: a leitura de KM e a manutenção já
        baixam o KM atual sozinhas ao serem corrigidas (km.correcao.ajustar_km_atual).
        """
        km = self.cleaned_data["km_atual"]
        if km is None:  # só no carro novo (campo opcional): o clean usa o KM da compra
            return km
        if self.instance.pk and km < self.instance.km_atual:
            raise ValidationError(
                f"KM atual ({milhar(km)}) menor que o registrado "
                f"({milhar(self.instance.km_atual)}). O KM do carro não diminui por aqui. "
                + _onde_corrigir_km(self.instance)
            )
        return km

    def clean_uso(self):
        """Carro alugado não pode virar "Fora de locação" (C14 — regra em
        impedimento_fora_de_locacao, usada também no Admin)."""
        uso = self.cleaned_data["uso"]
        if self.instance.pk and uso == Veiculo.Uso.FORA_LOCACAO:
            motivo = impedimento_fora_de_locacao(self.instance)
            if motivo:
                raise ValidationError(motivo)
        return uso

    def clean(self):
        dados = super().clean()
        km_compra = dados.get("km_compra")
        km_atual = dados.get("km_atual")
        if not self.instance.pk and not km_atual and "km_atual" not in self.errors:
            # carro novo sem KM atual: começa com o KM da compra
            dados["km_atual"] = km_atual = km_compra or 0
        if km_compra and km_atual is not None and km_atual < km_compra:
            self.add_error(
                "km_atual",
                f"O KM atual não pode ser menor que o KM na compra ({milhar(km_compra)}).",
            )
        # proteção mês a mês conta a partir da compra: sem a data, não há o que lançar
        if dados.get("mensalidade_protecao") and not dados.get("data_aquisicao"):
            self.add_error(
                "data_aquisicao", "Informe a data de compra para calcular a proteção mês a mês."
            )
        if (
            self.instance.pk
            and self.instance.uso == Veiculo.Uso.LOCACAO
            and dados.get("uso") == Veiculo.Uso.FORA_LOCACAO
            and not dados.get("confirmar_fora_locacao")
            and tem_lancamentos(self.instance)
        ):
            self.add_error(
                "confirmar_fora_locacao",
                "Carro “Fora de locação” não entra nos relatórios — nem nos meses que já "
                "passaram. Marque a caixinha para confirmar.",
            )
        return dados

    @property
    def secoes(self):
        return [(titulo, [self[nome] for nome in nomes]) for titulo, nomes in SECOES_VEICULO]


def _onde_corrigir_km(veiculo):
    """Diz onde está o lançamento com o KM atual do carro, para a pessoa corrigir lá.

    Corrigir a leitura de KM ou a manutenção baixa o KM atual sozinho
    (km.correcao.ajustar_km_atual).
    """
    from apps.km.models import RegistroKm
    from apps.manutencao.models import Manutencao

    km = veiculo.km_atual
    leitura = RegistroKm.objects.filter(veiculo=veiculo, km=km).order_by("-mes_referencia").first()
    if leitura:
        return (
            f"Ele veio da leitura de KM de {leitura.mes_referencia:%m/%Y}: se estiver errada, "
            "abra “Registros de KM” na ficha deste carro e use “Corrigir” nela — o KM atual "
            "se ajusta sozinho."
        )
    manutencao = Manutencao.objects.filter(veiculo=veiculo, km=km).order_by("-data").first()
    if manutencao:
        return (
            f"Ele veio da manutenção de {manutencao.data:%d/%m/%Y}: se o KM dela estiver errado, "
            "abra “Manutenções” na ficha deste carro e use “Editar” nela — o KM atual "
            "se ajusta sozinho."
        )
    # sem leitura/manutenção com esse número (entrega, devolução, vistoria ou o
    # próprio cadastro): a orientação geral de sempre
    return (
        "Se uma leitura foi digitada errada, abra “Registros de KM” na ficha deste carro "
        "e use “Corrigir” nela."
    )


def tem_lancamentos(veiculo):
    """O carro tem gastos ou cobranças lançados (que hoje aparecem nos relatórios)?"""
    return (
        veiculo.manutencoes.exists()
        or veiculo.pagamentos_ipva.exists()
        or veiculo.multas.exists()
        or veiculo.sinistros.exists()
        or veiculo.alocacoes.filter(cobrancas__isnull=False).exists()
    )


def impedimento_fora_de_locacao(veiculo):
    """Por que o carro não pode virar "Fora de locação" agora (None = pode).

    Ele continuaria sendo cobrado, mas sairia das preventivas, do controle de
    KM e do ranking de venda enquanto o motorista roda com ele (C14).
    """
    if veiculo.alocacoes.filter(status=Alocacao.Status.ATIVA).exists():
        return "Este carro está alugado. Encerre a alocação antes de marcar como “Fora de locação”."
    if TrocaTemporaria.objects.filter(
        Q(veiculo_substituto=veiculo) | Q(alocacao__veiculo=veiculo),
        data_devolucao__isnull=True,
    ).exists():
        return (
            "Este carro está numa troca (carro reserva) em aberto. Registre a devolução "
            "da troca antes de marcar como “Fora de locação”."
        )
    return None


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


class PagamentoIpvaForm(FormularioBR, forms.ModelForm):
    class Meta:
        model = PagamentoIpva
        fields = ["ano", "parcela", "valor", "vencimento", "pago_em"]
        widgets = {
            "ano": forms.NumberInput(attrs={"inputmode": "numeric", "min": 2000}),
            "parcela": forms.NumberInput(attrs={"inputmode": "numeric", "min": 1}),
            "vencimento": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "pago_em": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        texto_ano = "Use o ano com 4 dígitos (ex.: 2026)"
        self.fields["ano"].validators += [
            MinValueValidator(2000, texto_ano),
            MaxValueValidator(timezone.localdate().year + 1, texto_ano),
        ]
        self.fields["ano"].error_messages["invalid"] = texto_ano
        self.fields["parcela"].validators.append(
            MinValueValidator(1, "A parcela começa em 1 (use 1 para cota única).")
        )

    def clean(self):
        dados = super().clean()
        ano, parcela = dados.get("ano"), dados.get("parcela")
        if self.instance.pk is None and dados.get("valor") is None and "valor" not in self.errors:
            self.add_error("valor", "Informe o valor do IPVA.")
        elif dados.get("pago_em") and dados.get("valor") is None:
            self.add_error("valor", "Informe quanto foi pago — sem valor não entra na despesa.")
        veiculo_id = self.instance.veiculo_id
        if (
            veiculo_id
            and ano
            and parcela
            and "parcela" not in self.errors
            and PagamentoIpva.objects.filter(veiculo_id=veiculo_id, ano=ano, parcela=parcela)
            .exclude(pk=self.instance.pk)
            .exists()
        ):
            self.add_error(
                "parcela",
                f"O IPVA {ano} parcela {parcela} deste carro já está lançado — "
                "edite a linha dele acima.",
            )
        return dados

    def validate_unique(self):
        # a duplicidade com o banco já vira a mensagem acima (sem repetir a genérica)
        if "parcela" not in self.errors:
            super().validate_unique()


IpvaFormSet = forms.inlineformset_factory(
    Veiculo, PagamentoIpva, form=PagamentoIpvaForm, extra=1, can_delete=True
)


def ipva(request, veiculo_id):
    """IPVA do carro ano a ano (e parcela a parcela) — decisão nº 21.

    Cada parcela paga vira despesa no mês do pagamento; lançar o ano novo não
    apaga os anteriores, então os relatórios já enviados continuam iguais.
    """
    veiculo = get_object_or_404(Veiculo, pk=veiculo_id)
    ano_atual = timezone.localdate().year
    parcelas_do_ano = veiculo.pagamentos_ipva.filter(ano=ano_atual).values_list(
        "parcela", flat=True
    )
    formset = IpvaFormSet(
        request.POST or None,
        instance=veiculo,
        queryset=veiculo.pagamentos_ipva.all(),
        # só a linha em branco nova: ano atual e a próxima parcela ainda livre
        initial=[{"ano": ano_atual, "parcela": max(parcelas_do_ano, default=0) + 1}],
    )
    if request.method == "POST" and formset.is_valid():
        if not formset.has_changed():
            messages.warning(request, "Nada foi lançado — preencha ano, parcela e valor.")
            return render(request, "frota/ipva.html", {"veiculo": veiculo, "formset": formset})
        formset.save()
        messages.success(request, f"IPVA do {veiculo.placa} salvo.")
        return redirect("frota:detalhe", veiculo.pk)
    return render(request, "frota/ipva.html", {"veiculo": veiculo, "formset": formset})


class CategoriaForm(FormularioBR, forms.ModelForm):
    class Meta:
        model = Categoria
        fields = ["nome", "valor_semanal_referencia", "observacoes"]
        widgets = {"observacoes": forms.Textarea(attrs={"rows": 2})}


class FornecedorForm(FormularioBR, forms.ModelForm):
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
            acao = "atualizado" if instancia else "cadastrado"
            messages.success(request, f"“{salvo.nome}” {acao} com sucesso.")
            return redirect(request.path)
        detalhes = "; ".join(
            f"{form.fields[campo].label if campo in form.fields else 'Formulário'}: {erros[0]}"
            for campo, erros in form.errors.items()
        )
        if instancia:
            # erro na edição de uma linha da tabela: o formulário "Novo" volta limpo,
            # senão ele apareceria preenchido com a linha editada (e criaria uma cópia).
            messages.error(request, f"“{instancia.nome}” não foi salvo — {detalhes}")
            form = form_classe()
        else:
            messages.error(request, f"Não foi possível cadastrar — {detalhes}")
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
                data=ler_data(request.POST.get("data"), "Data da venda"),
                # aceita "45.000,00", "45000,00" ou "R$ 45.000" (apps/campos.py)
                valor=dinheiro_br(request.POST["valor"]),
                comprador=request.POST.get("comprador", ""),
                custos=(
                    dinheiro_br(request.POST["custos"])
                    if request.POST.get("custos", "").strip()
                    else None
                ),
                km=(
                    int(request.POST["km"].replace(".", "").strip())
                    if request.POST.get("km", "").strip()
                    else None
                ),
            )
            messages.success(
                request,
                f"{veiculo.placa} vendido — crédito da venda fica fora da base do DAS. "
                "Resultado final na ficha do veículo.",
            )
            return redirect("frota:ficha", veiculo.pk)
        except (ValidationError, KeyError, InvalidOperation, ValueError) as erro:
            # registrar_venda pode ter preenchido os campos da venda no objeto antes
            # de recusar: sem isto a ficha abaixo mostraria um "resultado final" falso
            veiculo.refresh_from_db()
            mensagens = getattr(
                erro,
                "messages",
                ["Confira a data e os valores — ex.: valor 45.000,00 e KM só com números."],
            )
            messages.error(request, "Venda não registrada: " + "; ".join(mensagens))
    if veiculo.status == Veiculo.Status.VENDIDO:
        impedimento = {
            "texto": f"{veiculo.placa} já foi vendido.",
            "url": reverse("frota:ficha", args=[veiculo.pk]),
            "acao": "Ver a ficha do carro",
        }
    else:
        # mesma explicação do detalhe: sem formulário enquanto alguém está com o carro
        impedimento = _quem_esta_com_o_carro(veiculo)["impedimento_venda"]
    dados = request.POST if request.method == "POST" else {}
    estimado = veiculo.valor_venda_estimado
    return render(
        request,
        "frota/vender.html",
        {
            "veiculo": veiculo,
            "hoje": timezone.localdate(),
            "dados": dados,
            "ficha": desmobilizacao.montar_ficha(veiculo),
            "impedimento_venda": impedimento,
            # já vem com o valor de mercado estimado; a pessoa ajusta para o recebido
            "valor_sugerido": numero_br(estimado) if estimado and not dados else "",
        },
    )


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
