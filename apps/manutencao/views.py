from uuid import uuid4

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import transaction
from django.db.models import Max
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.alocacoes.km_aviso import suspeita_de_km
from apps.alocacoes.models import VALIDADOR_KM
from apps.campos import FormularioBR
from apps.financeiro.models import Cobranca
from apps.financeiro.views import _envio_unico
from apps.formatos import brl, milhar
from apps.frota.models import Veiculo
from apps.km.correcao import ajustar_km_atual

from .models import IntervaloPersonalizado, ItemPreventiva, Manutencao
from .repasse import cliente_do_repasse, gerar_repasse
from .services import (
    STATUS_FORA_DO_ACOMPANHAMENTO,
    StatusPreventiva,
    frota_acompanhada,
    oficina_por_veiculo,
    pode_voltar_a_disponivel,
    resumos_por_veiculo,
)

#: custo e cobrança são valores pagos/recebidos: negativo viraria "lucro" falso no histórico
SEM_NEGATIVO = MinValueValidator(0, message="O valor não pode ser negativo.")

#: chave da sessão com os códigos de formulário de "Registrar manutenção" já gravados
SESSAO_TOKENS_MANUTENCAO = "manutencao_tokens_usados"


def _ultima_anotacao_de_km(veiculo, ignorar=None):
    """Data mais recente em que o KM do carro foi anotado (leitura, vistoria ou
    manutenção), ou None se nunca foi."""
    from apps.alocacoes.models import Vistoria
    from apps.km.models import RegistroKm

    manutencoes = Manutencao.objects.filter(veiculo=veiculo, km__isnull=False)
    if ignorar is not None and ignorar.pk:
        manutencoes = manutencoes.exclude(pk=ignorar.pk)
    datas = [
        RegistroKm.objects.filter(veiculo=veiculo).aggregate(d=Max("data_leitura"))["d"],
        Vistoria.objects.filter(veiculo=veiculo, km__isnull=False).aggregate(d=Max("data"))["d"],
        manutencoes.aggregate(d=Max("data"))["d"],
    ]
    datas = [d for d in datas if d]
    return max(datas) if datas else None


def _km_menor_que_o_atual(veiculo, km, data, instancia=None):
    """Aviso quando o KM digitado é menor que o que o carro já marcou antes desta data.

    Ex.: 5.000 digitado num carro que já marcou 50.000 — faltou um dígito, e a
    preventiva ficaria "Vencida" na hora. Manutenção retroativa (de antes do
    lançamento que marcou o KM atual) é normal e não avisa.
    """
    from apps.km.correcao import origem_do_km

    if instancia is not None and instancia.pk and instancia.km == veiculo.km_atual:
        return None  # é esta manutenção que marcou o KM atual: corrigir para menos é normal
    _rotulo, data_base = origem_do_km(veiculo, veiculo.km_atual)
    if data_base is None:
        data_base = _ultima_anotacao_de_km(veiculo, ignorar=instancia)
    if data_base is None:
        # Só o KM do cadastro: a última troca feita antes de o carro entrar no
        # sistema é normal ("informar a última"), mas menos da metade é dígito faltando.
        if km * 2 >= veiculo.km_atual:
            return None
        return (
            f"Você digitou {milhar(km)} km, mas o carro está cadastrado com "
            f"{milhar(veiculo.km_atual)} km. Confira se faltou um dígito; se estiver certo, "
            f"marque “Confirmo o KM”."
        )
    if data < data_base:
        return None
    return (
        f"Você digitou {milhar(km)} km, mas o carro já marcou {milhar(veiculo.km_atual)} km "
        f"em {data_base:%d/%m}. Confira se faltou um dígito; se estiver certo, marque "
        f"“Confirmo o KM”."
    )


def suspeita_do_km_da_manutencao(veiculo, km, data, instancia=None):
    """Aviso quando o KM digitado foge do normal (D27); None se ok.

    Acima do KM atual: mesma régua da vistoria e da leitura mensal (é ele que
    mexe no carro — preventivas "Vencida", excedente). Abaixo: avisa quando a
    manutenção é de depois do lançamento que marcou o KM atual.
    """
    from apps.km.correcao import SALTO_MAXIMO_SEM_LEITURA

    if veiculo is None or km is None or data is None:
        return None
    if instancia is not None and instancia.pk and instancia.km == km:
        return None  # editando sem mexer no KM: já foi aceito antes
    if km < veiculo.km_atual:
        return _km_menor_que_o_atual(veiculo, km, data, instancia)
    if km == veiculo.km_atual:
        return None
    data_base = _ultima_anotacao_de_km(veiculo, ignorar=instancia)
    if data_base is not None:
        return suspeita_de_km(km, data, veiculo.km_atual, data_base, "na última anotação de KM")
    if km - veiculo.km_atual <= SALTO_MAXIMO_SEM_LEITURA:
        return None
    return (
        f"Você digitou {milhar(km)} km, mas o carro está cadastrado com "
        f"{milhar(veiculo.km_atual)} km — seriam {milhar(km - veiculo.km_atual)} km a mais. "
        f"Confira se não sobrou um dígito. Se estiver certo, marque “Confirmo o KM” e "
        f"salve de novo."
    )


def _datas_da_oficina(entrada, saida):
    """Erros de datas da oficina que valem no registro e no "Carro voltou": {campo: texto}."""
    hoje = timezone.localdate()
    erros = {}
    if entrada and entrada > hoje:
        # entrada no futuro deixaria o selo "Na oficina" com dias parado negativos
        erros["data_entrada"] = "A entrada na oficina não pode ser depois de hoje."
    if saida and saida > hoje:
        erros["data_saida"] = "A data de saída não pode ser depois de hoje."
    elif entrada and saida and saida < entrada:
        erros["data_saida"] = f"A saída não pode ser antes da entrada ({entrada:%d/%m/%Y})."
    return erros


class ManutencaoForm(FormularioBR, forms.ModelForm):
    """Registro e edição de manutenção, em três partes (o que foi feito, oficina, custo)."""

    #: só faz sentido quando o custo é do cliente; cria a cobrança ao salvar
    gerar_cobranca = forms.BooleanField(
        label="Já gerar a cobrança para o cliente",
        required=False,
        help_text="A cobrança entra no Financeiro como conta a receber.",
    )
    #: só aparece na tela quando o KM foge do normal (D27)
    confirmar_km = forms.BooleanField(
        label="Confirmo o KM (está certo, mesmo fora do normal)",
        required=False,
    )

    class Meta:
        model = Manutencao
        fields = [
            "item",
            "data",
            "km",
            "descricao",
            "oficina",
            "data_entrada",
            "data_saida",
            "sinistro",
            "origem_custo",
            "custo_real",
            "pagamento_custo",
            "responsavel",
            "valor_cobrado_cliente",
            "observacoes",
        ]
        labels = {
            "item": "Item do plano",
            "km": "KM do carro no dia",
            "data_entrada": "Entrou na oficina em",
            "data_saida": "Saiu da oficina em",
            "custo_real": "Quanto a oficina cobrou (R$)",
            "pagamento_custo": "Já pagou a oficina?",
            "responsavel": "Quem paga o conserto",
            "valor_cobrado_cliente": "Valor a cobrar do cliente (R$)",
        }
        help_texts = {
            "item": "Escolha quando for uma revisão do plano (óleo, pneus…): "
            "a contagem da próxima troca recomeça deste KM.",
            "data_entrada": "Preencha para contar os dias parado "
            "(auxílio ao motorista após 7 dias).",
            "data_saida": "Deixe vazio enquanto o carro estiver na oficina.",
        }
        widgets = {
            "data": forms.DateInput(attrs={"type": "date"}),
            "data_entrada": forms.DateInput(attrs={"type": "date"}),
            "data_saida": forms.DateInput(attrs={"type": "date"}),
            "descricao": forms.Textarea(attrs={"rows": 3}),
            "observacoes": forms.Textarea(attrs={"rows": 2}),
            "km": forms.NumberInput(attrs={"inputmode": "numeric"}),
        }

    def __init__(self, *args, veiculo=None, **kwargs):
        super().__init__(*args, **kwargs)
        veiculo = veiculo or (self.instance.veiculo if self.instance.veiculo_id else None)
        self.veiculo = veiculo
        self.km_suspeito = False
        self.fields["km"].validators.append(VALIDADOR_KM)
        self.fields["item"].queryset = ItemPreventiva.objects.filter(ativo=True)
        self.fields["pagamento_custo"].choices = [
            ("pendente", "Ainda não"),
            ("pago", "Sim, já pagou"),
        ]
        if veiculo is not None:
            self.fields["sinistro"].queryset = veiculo.sinistros.all()
        for nome in ("custo_real", "valor_cobrado_cliente"):
            self.fields[nome].validators.append(SEM_NEGATIVO)
        if self.instance.cobranca_repasse_id:
            # Cobrança já gerada: valor e responsável ficam como foram cobrados.
            for nome in ("responsavel", "valor_cobrado_cliente"):
                self.fields[nome].disabled = True
            del self.fields["gerar_cobranca"]

    def tipos_por_item(self):
        """{item_id: tipo} — o formulário escolhe o tipo sozinho ao escolher o item."""
        return {
            str(item.pk): (
                Manutencao.Tipo.PREVENTIVA
                if item.intervalo_km_padrao
                else Manutencao.Tipo.ESPORADICA
            )
            for item in self.fields["item"].queryset
        }

    def tipo_escolhido(self, dados):
        """O tipo sai do item (mesma regra de tipos_por_item) — não se escolhe à mão.

        Item com intervalo → preventiva; item sem intervalo → esporádica; sem
        item → corretiva (conserto, inclusive o do atalho de sinistro).
        """
        item = dados.get("item")
        if item is None:
            return Manutencao.Tipo.CORRETIVA
        if item.intervalo_km_padrao:
            return Manutencao.Tipo.PREVENTIVA
        return Manutencao.Tipo.ESPORADICA

    def clean(self):
        dados = super().clean()
        item = dados.get("item")
        self.instance.tipo = self.tipo_escolhido(dados)
        if item and item.intervalo_km_padrao and dados.get("km") is None:
            self.add_error("km", "Informe o KM para a contagem da próxima troca recomeçar.")
        aviso_km = suspeita_do_km_da_manutencao(
            self.veiculo, dados.get("km"), dados.get("data"), self.instance
        )
        # a caixinha só aparece quando o aviso é de KM fora do normal (outro erro
        # de KM não se resolve marcando "Confirmo") e segue marcada se a pessoa
        # já confirmou mas outro campo deu erro
        self.km_suspeito = bool(aviso_km)
        if aviso_km and not dados.get("confirmar_km"):
            self.add_error("km", aviso_km)
        entrada, saida = dados.get("data_entrada"), dados.get("data_saida")
        if saida and not entrada:
            self.add_error("data_entrada", "Informe também quando o carro entrou na oficina.")
        for campo, erro in _datas_da_oficina(entrada, saida).items():
            self.add_error(campo, erro)
        if self.instance.tipo == Manutencao.Tipo.PREVENTIVA:
            dados["sinistro"] = None  # campo escondido nesse tipo: não grava sobra
        if (
            "valor_cobrado_cliente" in self.fields
            and not self.fields["valor_cobrado_cliente"].disabled
            and dados.get("responsavel") != Manutencao.Responsavel.CLIENTE
        ):
            dados["valor_cobrado_cliente"] = None  # escondido quando a empresa paga
            dados["gerar_cobranca"] = False
        if dados.get("gerar_cobranca") and not dados.get("valor_cobrado_cliente"):
            self.add_error("valor_cobrado_cliente", "Informe o valor para gerar a cobrança.")
        return dados


class VoltouDaOficinaForm(FormularioBR, forms.ModelForm):
    """ "Carro voltou da oficina": data de saída, custo final e pagamento."""

    class Meta:
        model = Manutencao
        fields = ["data_saida", "custo_real", "pagamento_custo"]
        labels = {
            "data_saida": "Saiu da oficina em",
            "custo_real": "Quanto a oficina cobrou (R$)",
            "pagamento_custo": "Já pagou a oficina?",
        }
        help_texts = {"custo_real": "Zero se a proteção cobriu."}
        widgets = {"data_saida": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["data_saida"].required = True
        self.fields["custo_real"].validators.append(SEM_NEGATIVO)
        self.fields["pagamento_custo"].choices = [
            ("pendente", "Ainda não"),
            ("pago", "Sim, já pagou"),
        ]

    def clean_data_saida(self):
        saida = self.cleaned_data["data_saida"]
        erro = _datas_da_oficina(self.instance.data_entrada, saida).get("data_saida")
        if erro:
            raise forms.ValidationError(erro)
        return saida


class ItemPreventivaForm(FormularioBR, forms.ModelForm):
    """Item do plano — com intervalo vira preventiva por km, sem intervalo é esporádica."""

    class Meta:
        model = ItemPreventiva
        fields = ["nome", "intervalo_km_padrao", "ativo"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 0 seria descartado no cálculo (falsy) e mentiria na tela.
        self.fields["intervalo_km_padrao"].validators.append(MinValueValidator(1))
        self.fields["intervalo_km_padrao"].widget.attrs["min"] = 1

    def clean_intervalo_km_padrao(self):
        intervalo = self.cleaned_data.get("intervalo_km_padrao")
        if intervalo is None and self.instance.pk and self.instance.intervalos.exists():
            raise forms.ValidationError(
                "Este item tem intervalos personalizados por veículo — remova-os "
                "antes de torná-lo esporádico, senão eles seriam ignorados em silêncio."
            )
        return intervalo


class IntervaloPersonalizadoForm(FormularioBR, forms.ModelForm):
    """Intervalo de km de um item para um veículo específico (docs.md §4.5)."""

    class Meta:
        model = IntervaloPersonalizado
        fields = ["veiculo", "item", "intervalo_km"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["veiculo"].queryset = Veiculo.objects.filter(uso=Veiculo.Uso.LOCACAO).exclude(
            status=Veiculo.Status.VENDIDO
        )
        self.fields["item"].queryset = ItemPreventiva.objects.filter(
            intervalo_km_padrao__isnull=False
        )
        self.fields["intervalo_km"].validators.append(MinValueValidator(1))
        self.fields["intervalo_km"].widget.attrs["min"] = 1


def _pk_do_post(request, chave):
    """IDs de POST adulterados viram 404, não 500."""
    valor = (request.POST.get(chave) or "").strip()
    if not valor.isdigit():
        raise Http404("Registro inválido.")
    return int(valor)


def _mensagem_de_erros(form):
    return "; ".join(erro for erros in form.errors.values() for erro in erros)


def plano(request):
    """Plano de manutenção editável: itens do plano e intervalos por veículo (docs.md §4.5)."""
    form_item = ItemPreventivaForm()
    form_intervalo = IntervaloPersonalizadoForm()
    if request.method == "POST":
        acao = request.POST.get("acao")
        if acao == "editar_item":
            item = get_object_or_404(ItemPreventiva, pk=_pk_do_post(request, "item_id"))
            form = ItemPreventivaForm(request.POST, instance=item)
            if form.is_valid():
                form.save()
                messages.success(request, f"Item {item.nome} atualizado.")
            else:
                messages.error(request, _mensagem_de_erros(form))
            return redirect("manutencao:plano")
        if acao == "novo_item":
            form_item = ItemPreventivaForm(request.POST)
            if form_item.is_valid():
                item = form_item.save()
                messages.success(request, f"Item {item.nome} incluído no plano.")
                return redirect("manutencao:plano")
        elif acao == "novo_intervalo":
            form_intervalo = IntervaloPersonalizadoForm(request.POST)
            if form_intervalo.is_valid():
                intervalo = form_intervalo.save()
                messages.success(request, f"Intervalo personalizado: {intervalo}.")
                return redirect("manutencao:plano")
        elif acao == "remover_intervalo":
            intervalo = get_object_or_404(
                IntervaloPersonalizado, pk=_pk_do_post(request, "intervalo_id")
            )
            intervalo.delete()
            messages.success(
                request,
                f"Intervalo personalizado removido — {intervalo.item.nome} volta ao padrão.",
            )
            return redirect("manutencao:plano")
    return render(
        request,
        "manutencao/plano.html",
        {
            "itens": ItemPreventiva.objects.all(),
            "personalizados": IntervaloPersonalizado.objects.select_related(
                "veiculo", "item"
            ).order_by("veiculo__placa", "item__nome"),
            "form_item": form_item,
            "form_intervalo": form_intervalo,
        },
    )


def preventivas(request):
    """Plano de preventivas da frota — status por veículo × item (docs.md §4.5).

    Abre só no que precisa de atenção (vencidas e perto de vencer), da mais
    atrasada para a menos; "?ver=todas" mostra a frota inteira por placa.
    """
    ver_todas = request.GET.get("ver") == "todas"
    veiculos = list(frota_acompanhada().order_by("placa"))
    resumos = resumos_por_veiculo(veiculos)  # queries fixas, não 6 por veículo
    quadro = [(veiculo, resumos[veiculo.pk]) for veiculo in veiculos]
    todos_itens = [p for _, itens in quadro for p in itens]
    if not ver_todas:
        quadro = [(v, [p for p in itens if p.em_alerta]) for v, itens in quadro]
        quadro = [(v, sorted(itens, key=lambda p: p.faltam_km)) for v, itens in quadro if itens]
        quadro.sort(key=lambda par: par[1][0].faltam_km)  # o carro mais atrasado primeiro
    na_oficina = oficina_por_veiculo([v.pk for v in veiculos])
    return render(
        request,
        "manutencao/preventivas.html",
        {
            "quadro": quadro,
            "Status": StatusPreventiva,
            "ver_todas": ver_todas,
            "total_em_dia": sum(1 for p in todos_itens if p.status == StatusPreventiva.OK),
            "total_sem_registro": sum(
                1 for p in todos_itens if p.status == StatusPreventiva.SEM_REGISTRO
            ),
            "na_oficina": sorted(na_oficina.values(), key=lambda m: m.data_entrada),
            # "Qual carro?": todos os ativos, sem depender do filtro de atenção
            "carros": Veiculo.objects.exclude(status__in=STATUS_FORA_DO_ACOMPANHAMENTO).order_by(
                "placa"
            ),
        },
    )


def _volta(request, padrao):
    """Para onde voltar depois de salvar: a tela de onde a pessoa veio (só deste site)."""
    destino = request.POST.get("volta") or request.GET.get("volta") or ""
    # só caminho deste site ("/frota/..."): "historico" sem barra virava erro 500 no redirect
    if (
        destino.startswith("/")
        and not destino.startswith("//")
        and url_has_allowed_host_and_scheme(
            destino, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        )
    ):
        return destino
    return padrao


def _avisar_cobranca(request, manutencao, gerar):
    """Gera o repasse quando pedido; senão lembra que falta cobrar o cliente."""
    if manutencao.responsavel != Manutencao.Responsavel.CLIENTE:
        return
    if not manutencao.valor_cobrado_cliente or manutencao.cobranca_repasse_id:
        return
    if gerar:
        try:
            cobranca = gerar_repasse(manutencao)
            messages.success(
                request, f"Cobrança de {brl(cobranca.valor)} criada para {cobranca.cliente.nome}."
            )
        except ValidationError as erro:
            messages.error(request, "; ".join(erro.messages))
        return
    messages.warning(
        request,
        "Custo do cliente — gere a cobrança no histórico do carro (botão “Cobrar do cliente”).",
    )


def _carro_voltou(request, manutencao):
    """Ao fechar a ida à oficina, o carro marcado "Em manutenção" volta a ficar disponível."""
    veiculo = manutencao.veiculo
    if pode_voltar_a_disponivel(veiculo):
        veiculo.status = Veiculo.Status.DISPONIVEL
        veiculo.save(update_fields=["status"])
        messages.info(request, f"{veiculo.placa} voltou a ficar disponível para alugar.")


def _carro_foi_para_oficina(request, manutencao):
    """Carro livre que entra na oficina sai das listas de aluguel e troca.

    Só o carro "Disponível" muda para "Em manutenção"; o alugado continua
    "Alocado" e ganha só o selo "Na oficina". Volta sozinho no "Carro voltou".
    """
    if not manutencao.data_entrada or manutencao.data_saida:
        return
    veiculo = manutencao.veiculo
    if veiculo.status == Veiculo.Status.DISPONIVEL:
        veiculo.status = Veiculo.Status.EM_MANUTENCAO
        veiculo.save(update_fields=["status"])
        messages.info(
            request, f"{veiculo.placa} ficou “Em manutenção” e não aparece para alugar até voltar."
        )


def registrar(request, veiculo_id):
    veiculo = get_object_or_404(Veiculo, pk=veiculo_id)
    # "Informar última troca" (item nunca registrado): a troca foi no passado, então
    # data e KM ficam em branco — com hoje e o KM atual, salvar sem olhar marcaria
    # o item como "Em dia" sem ter sido feito.
    informar_ultima = request.GET.get("ultima") == "1"
    inicial = {} if informar_ultima else {"data": timezone.localdate(), "km": veiculo.km_atual}
    item_id = request.GET.get("item", "")
    if item_id.isdigit():
        item = ItemPreventiva.objects.filter(pk=item_id, ativo=True).first()
        if item:
            inicial["item"] = item.pk
            inicial["descricao"] = item.nome
    sinistro_id = request.GET.get("sinistro", "")
    if sinistro_id.isdigit() and veiculo.sinistros.filter(pk=sinistro_id).exists():
        inicial.update(sinistro=int(sinistro_id), data_entrada=timezone.localdate())
    # Cada formulário leva um código único (como a ND e a multa): clique duplo,
    # internet lenta ou "Voltar" não gravam a mesma manutenção duas vezes.
    token = request.POST.get("token", "") if request.method == "POST" else ""
    form = ManutencaoForm(request.POST or None, veiculo=veiculo, initial=inicial)
    if request.method == "POST" and form.is_valid():
        with _envio_unico(request, SESSAO_TOKENS_MANUTENCAO, token) as repetido:
            if repetido:
                messages.warning(
                    request,
                    "Esta manutenção já tinha sido registrada — o envio repetido foi ignorado "
                    "e nada foi gravado em dobro.",
                )
                return redirect(_volta(request, reverse("manutencao:historico", args=[veiculo.pk])))
            manutencao = form.save(commit=False)
            manutencao.veiculo = veiculo
            manutencao.save()
        messages.success(request, f"Manutenção registrada para {veiculo.placa}.")
        _avisar_cobranca(request, manutencao, form.cleaned_data.get("gerar_cobranca"))
        _carro_foi_para_oficina(request, manutencao)
        if manutencao.data_saida:
            _carro_voltou(request, manutencao)
        return redirect(_volta(request, reverse("manutencao:historico", args=[veiculo.pk])))
    return render(
        request,
        "manutencao/registrar.html",
        {
            "veiculo": veiculo,
            "form": form,
            "volta": _volta(request, ""),
            "informar_ultima": informar_ultima,
            "token": token or uuid4().hex,
        },
    )


def editar(request, manutencao_id):
    """Corrige uma manutenção já registrada (custo final, data de saída…)."""
    manutencao = get_object_or_404(Manutencao.objects.select_related("veiculo"), pk=manutencao_id)
    veiculo = manutencao.veiculo
    estava_aberta = manutencao.data_entrada and not manutencao.data_saida
    km_antigo = manutencao.km
    form = ManutencaoForm(request.POST or None, instance=manutencao)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            manutencao = form.save()
            aviso_km = None
            if km_antigo is not None and manutencao.km != km_antigo:
                # KM corrigido para menos (ou apagado): o KM atual do carro não fica
                # preso no número errado (preventivas "Vencida" para sempre).
                aviso_km = ajustar_km_atual(veiculo, km_antigo)
        messages.success(request, f"Manutenção de {veiculo.placa} atualizada.")
        if aviso_km:
            messages.info(request, aviso_km)
        _avisar_cobranca(request, manutencao, form.cleaned_data.get("gerar_cobranca"))
        _carro_foi_para_oficina(request, manutencao)
        segue_aberta = manutencao.data_entrada and not manutencao.data_saida
        if estava_aberta and not segue_aberta:
            # saída informada — ou a entrada apagada por engano: o carro não fica preso
            _carro_voltou(request, manutencao)
        return redirect(_volta(request, reverse("manutencao:historico", args=[veiculo.pk])))
    return render(
        request,
        "manutencao/registrar.html",
        {"veiculo": veiculo, "form": form, "editando": manutencao, "volta": _volta(request, "")},
    )


@require_POST
def apagar(request, manutencao_id):
    """Apaga uma manutenção lançada por engano e desfaz o que ela mudou no carro."""
    manutencao = get_object_or_404(
        Manutencao.objects.select_related("veiculo", "cobranca_repasse"), pk=manutencao_id
    )
    veiculo = manutencao.veiculo
    historico_url = reverse("manutencao:historico", args=[veiculo.pk])
    cobranca = manutencao.cobranca_repasse
    if cobranca is not None and cobranca.status != Cobranca.Status.CANCELADA:
        messages.error(
            request,
            f"Esta manutenção já gerou cobrança de {brl(cobranca.valor)} para o cliente. "
            f"Cancele a cobrança no Financeiro antes de apagar.",
        )
        return redirect("manutencao:editar", manutencao.pk)
    km = manutencao.km
    estava_aberta = manutencao.data_entrada and not manutencao.data_saida
    with transaction.atomic():
        manutencao.delete()
        aviso_km = ajustar_km_atual(veiculo, km) if km is not None else None
        # só a ida à oficina apagada solta o carro (o "Em manutenção" à mão fica)
        voltou = bool(estava_aberta) and pode_voltar_a_disponivel(veiculo)
        if voltou:
            veiculo.status = Veiculo.Status.DISPONIVEL
            veiculo.save(update_fields=["status"])
    messages.success(request, f"Manutenção de {veiculo.placa} apagada.")
    if aviso_km:
        messages.info(request, aviso_km)
    if voltou:
        messages.info(request, f"{veiculo.placa} voltou a ficar disponível para alugar.")
    return redirect(_volta(request, historico_url))


def escolher_carro(request):
    """ "Qual carro?" do quadro de preventivas: leva ao registro do carro escolhido."""
    veiculo_id = request.GET.get("veiculo", "")
    if veiculo_id.isdigit() and Veiculo.objects.filter(pk=veiculo_id).exists():
        return redirect("manutencao:registrar", int(veiculo_id))
    messages.error(request, "Escolha o carro na lista.")
    return redirect("manutencao:preventivas")


def voltou(request, manutencao_id):
    """ "Carro voltou da oficina": fecha a ida à oficina sem preencher tudo de novo."""
    manutencao = get_object_or_404(Manutencao.objects.select_related("veiculo"), pk=manutencao_id)
    veiculo = manutencao.veiculo
    historico_url = reverse("manutencao:historico", args=[veiculo.pk])
    if manutencao.data_saida:
        messages.info(
            request, f"{veiculo.placa} já saiu da oficina em {manutencao.data_saida:%d/%m/%Y}."
        )
        return redirect(_volta(request, historico_url))
    if not manutencao.data_entrada:
        # sem entrada não há "ida à oficina" para fechar (nem dias parado para contar)
        messages.info(request, "Esta manutenção não tem data de entrada na oficina. Corrija aqui.")
        return redirect("manutencao:editar", manutencao.pk)
    form = VoltouDaOficinaForm(
        request.POST or None, instance=manutencao, initial={"data_saida": timezone.localdate()}
    )
    if request.method == "POST" and form.is_valid():
        manutencao = form.save()
        messages.success(
            request,
            f"{veiculo.placa} saiu da oficina — {manutencao.dias_parado} "
            f"dia{'s' if manutencao.dias_parado != 1 else ''} parado.",
        )
        _carro_voltou(request, manutencao)
        return redirect(_volta(request, historico_url))
    return render(
        request,
        "manutencao/voltou.html",
        {"manutencao": manutencao, "veiculo": veiculo, "form": form, "volta": _volta(request, "")},
    )


def repassar(request, manutencao_id):
    manutencao = get_object_or_404(Manutencao, pk=manutencao_id)
    if request.method == "POST":
        try:
            cobranca = gerar_repasse(manutencao)
            messages.success(
                request, f"Repasse de {brl(cobranca.valor)} cobrado de {cobranca.cliente.nome}."
            )
        except ValidationError as erro:
            messages.error(request, "; ".join(erro.messages))
    return redirect("manutencao:historico", manutencao.veiculo_id)


def historico(request, veiculo_id):
    veiculo = get_object_or_404(Veiculo, pk=veiculo_id)
    manutencoes = list(
        veiculo.manutencoes.select_related("item", "oficina", "sinistro__cliente").order_by("-data")
    )
    for m in manutencoes:
        # Quem será cobrado aparece no botão antes de confirmar o repasse.
        m.cliente_repasse = None
        if (
            not m.cobranca_repasse_id
            and m.responsavel == Manutencao.Responsavel.CLIENTE
            and m.valor_cobrado_cliente
        ):
            m.cliente_repasse = cliente_do_repasse(m)
    return render(
        request,
        "manutencao/historico.html",
        {"veiculo": veiculo, "manutencoes": manutencoes},
    )
