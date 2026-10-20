from datetime import date, timedelta

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme

from apps.campos import FormularioBR, exibir_dinheiro
from apps.financeiro.models import Caucao
from apps.financeiro.services import gerar_cobrancas_semanais
from apps.formatos import brl
from apps.frota.models import Veiculo
from apps.manutencao.services import oficina_por_veiculo
from apps.pessoas.models import Cliente

from . import checklist
from .entrega import situacao_da_devolucao
from .km_aviso import base_da_vistoria, suspeita_de_km
from .models import KM_MAXIMO, Alocacao, TrocaTemporaria, Vistoria
from .services import linha_do_tempo


def dividas_por_cliente(clientes_ids=None):
    """{cliente_id: {"total", "judicial", "vencido"}} do que cada um deve hoje (D17)."""
    from apps.financeiro.models import Cobranca  # import local evita ciclo de import

    cobrancas = Cobranca.objects.filter(status__in=Cobranca.STATUS_DEVIDOS)
    if clientes_ids is not None:
        cobrancas = cobrancas.filter(cliente_id__in=clientes_ids)
    hoje = date.today()
    dividas = {}
    for cobranca in Cobranca.com_quitacao_anotada(cobrancas):
        if cobranca.saldo <= 0:
            continue
        divida = dividas.setdefault(cobranca.cliente_id, {"total": 0, "judicial": 0, "vencido": 0})
        divida["total"] += cobranca.saldo
        if cobranca.status == Cobranca.Status.JUDICIAL:
            divida["judicial"] += cobranca.saldo
        if cobranca.vencimento < hoje or cobranca.status == Cobranca.Status.JUDICIAL:
            divida["vencido"] += cobranca.saldo
    return dividas


def _precisa_confirmar_divida(cliente, divida):
    return cliente.status == Cliente.Status.INADIMPLENTE or bool(divida and divida["vencido"])


def texto_da_divida(cliente, divida):
    """Frase do aviso de dívida (D17), ou None se não precisa de confirmação."""
    if not _precisa_confirmar_divida(cliente, divida):
        return None
    if not divida:
        return f"{cliente.nome} está marcado como inadimplente."
    texto = f"{cliente.nome} está devendo {brl(divida['total'])}"
    if divida["judicial"]:
        texto += f" ({brl(divida['judicial'])} em cobrança judicial)"
    return texto + "."


class AlocacaoForm(FormularioBR, forms.ModelForm):
    class Meta:
        model = Alocacao
        fields = [
            "veiculo",
            "cliente",
            "data_inicio",
            "cobrar_a_partir_de",
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
            "cobrar_a_partir_de": forms.DateInput(attrs={"type": "date"}),
            "observacoes": forms.Textarea(attrs={"rows": 2}),
        }

    #: Início mais antigo que isso = contrato que já rodava (carga inicial, D01).
    DIAS_INICIO_ANTIGO = 7

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["veiculo"].queryset = Veiculo.objects.filter(
            status=Veiculo.Status.DISPONIVEL, uso=Veiculo.Uso.LOCACAO
        )
        self.fields["cliente"].queryset = Cliente.objects.exclude(status=Cliente.Status.INATIVO)
        self.fields["veiculo"].empty_label = "Escolha o carro…"
        self.fields["cliente"].empty_label = "Escolha o cliente…"
        dia = self.fields["dia_vencimento"]
        dia.required = False
        dia.choices = [("", "Mesmo dia da semana do início"), *Cliente.DiaSemana.choices]
        # D17: cliente devendo pode alugar, mas só com confirmação explícita
        self.texto_divida = None
        cliente_id = str(self.data.get("cliente", "")) if self.is_bound else ""
        cliente = Cliente.objects.filter(pk=cliente_id).first() if cliente_id.isdigit() else None
        if cliente:
            self.texto_divida = texto_da_divida(
                cliente, dividas_por_cliente([cliente.pk]).get(cliente.pk)
            )
        if self.texto_divida:
            self.fields["confirmar_divida"] = forms.BooleanField(
                required=False, label="Confirmo: alocar mesmo com o cliente devendo"
            )

    def clean_confirmar_divida(self):
        confirmado = self.cleaned_data.get("confirmar_divida")
        if self.texto_divida and not confirmado:
            raise ValidationError(
                f"{self.texto_divida} Para entregar o carro mesmo assim, marque a confirmação."
            )
        return confirmado

    def clean_dia_vencimento(self):
        valor = self.cleaned_data.get("dia_vencimento")
        return None if valor in (None, "") else valor

    def clean(self):
        """D01: contrato antigo precisa dizer de quando o sistema passa a cobrar.

        Sem isso, cadastrar um contrato de março lança de uma vez todas as
        semanas desde março, todas atrasadas, e o motorista vira inadimplente.
        """
        dados = super().clean()
        inicio = dados.get("data_inicio")
        antigo = inicio and inicio < date.today() - timedelta(days=self.DIAS_INICIO_ANTIGO)
        if antigo and not dados.get("cobrar_a_partir_de"):
            self.add_error(
                "cobrar_a_partir_de",
                f"Este contrato começou em {inicio:%d/%m/%Y}, há mais de uma semana. "
                "Diga a partir de que dia o sistema deve cobrar o aluguel — as semanas "
                "antes disso não serão lançadas. Para cobrar tudo desde o início, "
                "repita aqui a data de início.",
            )
        cobrar = dados.get("cobrar_a_partir_de")
        base = max(date.today(), inicio) if inicio else date.today()
        if cobrar and cobrar > base + timedelta(days=self.DIAS_INICIO_ANTIGO):
            # ano digitado errado (2027) deixaria o carro rodando meses sem cobrança
            self.add_error(
                "cobrar_a_partir_de",
                f"{cobrar:%d/%m/%Y} está longe demais — confira a data. As semanas até "
                "lá não seriam cobradas.",
            )
        return dados


class AlocacaoEdicaoForm(FormularioBR, forms.ModelForm):
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

    def clean_caucao_valor(self):
        """Não some com a caução combinada enquanto houver dinheiro retido (C01)."""
        valor = self.cleaned_data.get("caucao_valor")
        if not valor and self.instance.pk:
            caucao = _caucao_com_saldo(self.instance)
            if caucao and caucao.saldo > 0:
                raise ValidationError(
                    f"Ainda há {brl(caucao.saldo)} retidos na caução deste cliente. "
                    "Devolva ou desconte esse valor em Financeiro > Cauções antes de "
                    "tirar a caução do contrato."
                )
        return valor


class TrocaForm(FormularioBR, forms.ModelForm):
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


class VistoriaForm(FormularioBR, forms.ModelForm):
    confirmar_km = forms.BooleanField(
        required=False,
        label="Confirmo o KM (está certo, mesmo fora do normal)",
    )

    class Meta:
        model = Vistoria
        fields = ["veiculo", "tipo", "data", "km", "combustivel", "avarias", "notas", "foto"]
        widgets = {
            "data": forms.DateInput(attrs={"type": "date"}),
            "avarias": forms.Textarea(attrs={"rows": 4}),
            "notas": forms.Textarea(attrs={"rows": 2}),
            "foto": forms.ClearableFileInput(
                attrs={"accept": ".jpg,.jpeg,.png,.webp,.pdf,image/jpeg,image/png,image/webp"}
            ),
        }

    def __init__(self, *args, alocacao, **kwargs):
        super().__init__(*args, **kwargs)
        self.alocacao = alocacao
        self.instance.alocacao = alocacao
        # D10: só o carro da locação e os substitutos dela; na troca, o substituto vem marcado
        campo = self.fields["veiculo"]
        campo.queryset = self.instance.veiculos_possiveis()
        # sem escolha (tela antiga, leitura da foto) vale o carro sugerido
        campo.required = False
        campo.empty_label = None
        campo.label_from_instance = lambda v: (
            f"{v.placa} — {v.marca_modelo}"
            + (" (carro da locação)" if v.pk == alocacao.veiculo_id else " (substituto)")
        )
        troca = alocacao.troca_ativa
        self.initial.setdefault(
            "veiculo", troca.veiculo_substituto_id if troca else alocacao.veiculo_id
        )

    def clean_veiculo(self):
        veiculo = self.cleaned_data.get("veiculo")
        if veiculo is None:
            troca = self.alocacao.troca_ativa
            veiculo = troca.veiculo_substituto if troca else self.alocacao.veiculo
        return veiculo

    def clean(self):
        dados = super().clean()
        veiculo, km, data = dados.get("veiculo"), dados.get("km"), dados.get("data")
        if veiculo and km and data:
            km_base, data_base, rotulo = base_da_vistoria(self.alocacao, veiculo)
            aviso = suspeita_de_km(km, data, km_base, data_base, rotulo)
            if aviso and not dados.get("confirmar_km"):
                self.add_error("km", aviso)
        return dados


def _caucao_com_saldo(alocacao):
    """Caução da alocação com o saldo já somado numa consulta só, ou None."""
    return Caucao.com_saldos_anotados(Caucao.objects.filter(alocacao=alocacao)).first()


def _avisar_erro_de_devolucao(request, erro):
    """Mensagem do formulário de data + KM (encerrar alocação / devolver substituto)."""
    mensagens = getattr(erro, "messages", ["Preencha a data e o KM corretamente."])
    messages.error(request, "; ".join(mensagens))


def _data_e_km_da_devolucao(post, campo_data):
    """Lê data e KM do POST; KM grande demais vira mensagem, não erro 500 (D27)."""
    data = date.fromisoformat(post[campo_data])
    km = int(post["km_devolucao"])
    if km > KM_MAXIMO:
        raise ValidationError("KM grande demais — confira os dígitos.")
    return data, km


def _ficha(alocacao):
    """C30: telas abertas da ficha do cliente voltam para ela."""
    return redirect("pessoas:detalhe", alocacao.cliente_id)


def lista(request):
    mostrar = request.GET.get("mostrar", "ativas")
    alocacoes = Alocacao.objects.select_related("veiculo", "cliente").prefetch_related(
        "trocas__veiculo_substituto"
    )
    if mostrar == "ativas":
        alocacoes = alocacoes.filter(status=Alocacao.Status.ATIVA)
    return render(request, "alocacoes/lista.html", {"alocacoes": alocacoes, "mostrar": mostrar})


def _sugestoes_da_nova_alocacao(form):
    """Dados que a tela usa para pré-preencher ao escolher carro e cliente.

    Carro → KM atual (já conhecido) e preço de referência da categoria.
    Cliente → dia de pagar e caução do cadastro, como SUGESTÃO editável
    (decisão #7, opção B: o valor que vale é o da alocação).
    """
    veiculos = {}
    for veiculo in form.fields["veiculo"].queryset.select_related("categoria"):
        dados = {"km": veiculo.km_atual}
        if veiculo.categoria and veiculo.categoria.valor_semanal_referencia:
            dados["categoria"] = veiculo.categoria.nome
            dados["valor"] = exibir_dinheiro(veiculo.categoria.valor_semanal_referencia)
        veiculos[str(veiculo.pk)] = dados
    dividas = dividas_por_cliente()
    clientes = {}
    for cliente in form.fields["cliente"].queryset:
        dados = {}
        if cliente.dia_vencimento is not None or cliente.caucao_referencia:
            dados = {
                "dia": "" if cliente.dia_vencimento is None else str(cliente.dia_vencimento),
                "dia_nome": cliente.get_dia_vencimento_display() or "",
                "caucao": exibir_dinheiro(cliente.caucao_referencia or None),
            }
        # D17: aviso já ao escolher o cliente — a confirmação é pedida ao salvar
        divida = texto_da_divida(cliente, dividas.get(cliente.pk))
        if divida:
            dados = {"dia": "", "dia_nome": "", "caucao": "", **dados, "divida": divida}
        if dados:
            clientes[str(cliente.pk)] = dados
    return veiculos, clientes


def nova(request):
    inicial = {"data_inicio": date.today()}
    veiculo_id = request.GET.get("veiculo", "")
    if veiculo_id.isdigit():
        inicial["veiculo"] = veiculo_id
        veiculo = Veiculo.objects.filter(pk=veiculo_id).only("km_atual").first()
        if veiculo:
            inicial["km_entrega"] = veiculo.km_atual
    cliente_id = request.GET.get("cliente", "")
    if cliente_id.isdigit():
        inicial["cliente"] = cliente_id
        cliente = Cliente.objects.filter(pk=cliente_id).first()
        if cliente:
            # sugestão do cadastro — a pessoa ainda pode mudar na tela
            inicial["dia_vencimento"] = cliente.dia_vencimento
            inicial["caucao_valor"] = cliente.caucao_referencia or None
    form = AlocacaoForm(request.POST or None, initial=inicial)
    if request.method == "POST" and form.is_valid():
        alocacao = form.save()  # sem dia de vencimento, o save usa o dia do início
        # Já cria a cobrança da 1ª semana (a rotina diária faria o mesmo amanhã):
        # o cartão "Próximos passos" mostra o valor e o botão de receber na hora.
        gerar_cobrancas_semanais()
        cliente = alocacao.cliente
        oficina = oficina_por_veiculo([alocacao.veiculo_id]).get(alocacao.veiculo_id)
        if oficina:
            messages.warning(
                request,
                f"Atenção: {alocacao.veiculo.placa} consta como na oficina desde "
                f"{oficina.data_entrada:%d/%m/%Y}. Se já voltou, marque "
                "“Carro voltou da oficina” no histórico de manutenção.",
            )
        if cliente.cnh_validade and cliente.cnh_validade < date.today():
            messages.warning(
                request,
                f"Atenção: a CNH de {cliente.nome} está vencida "
                f"desde {cliente.cnh_validade:%d/%m/%Y}.",
            )
        messages.success(
            request,
            f"Veículo {alocacao.veiculo.placa} alocado a {cliente.nome}. "
            "Siga os próximos passos da entrega logo abaixo.",
        )
        return redirect("pessoas:detalhe", cliente.pk)
    veiculos, clientes = _sugestoes_da_nova_alocacao(form)
    return render(
        request,
        "alocacoes/nova.html",
        {
            "form": form,
            "dados_veiculos": veiculos,
            "dados_clientes": clientes,
            # aviso ao escolher carro que ainda está na oficina (manutenção sem saída)
            "carros_na_oficina": {
                str(pk): f"{m.data_entrada:%d/%m}"
                for pk, m in oficina_por_veiculo(
                    form.fields["veiculo"].queryset.values_list("pk", flat=True)
                ).items()
            },
            "sem_veiculos": not form.fields["veiculo"].queryset.exists(),
            "sem_clientes": not form.fields["cliente"].queryset.exists(),
        },
    )


def entrega_dispensar(request, alocacao_id):
    """Esconde o cartão "Próximos passos da entrega" da ficha do cliente."""
    alocacao = get_object_or_404(Alocacao, pk=alocacao_id)
    if request.method == "POST":
        alocacao.passos_entrega_dispensados = True
        alocacao.save(update_fields=["passos_entrega_dispensados"])
        messages.info(request, "Cartão de próximos passos da entrega dispensado.")
    return redirect("pessoas:detalhe", alocacao.cliente_id)


def editar(request, alocacao_id):
    """Ajusta valores e condições de uma alocação sem mexer no vínculo carro↔cliente."""
    alocacao = get_object_or_404(
        Alocacao.objects.select_related("veiculo", "cliente"), pk=alocacao_id
    )
    if alocacao.status != Alocacao.Status.ATIVA:
        # Contrato encerrado é registro histórico — não se altera.
        messages.error(request, "Alocação encerrada não pode ser editada.")
        return redirect("alocacoes:lista")
    form = AlocacaoEdicaoForm(request.POST or None, instance=alocacao)
    if request.method == "POST" and form.is_valid():
        form.save()  # com caução, o signal do financeiro abre o registro (C01)
        if not alocacao.caucao_valor:
            # caução tirada do contrato: some o registro que nunca recebeu dinheiro
            Caucao.objects.filter(alocacao=alocacao, movimentacoes__isnull=True).delete()
        messages.success(
            request,
            f"Alocação de {alocacao.veiculo.placa} → {alocacao.cliente.nome} atualizada.",
        )
        return _ficha(alocacao)
    return render(request, "alocacoes/editar.html", {"alocacao": alocacao, "form": form})


def encerrar(request, alocacao_id):
    alocacao = get_object_or_404(
        Alocacao.objects.select_related("veiculo", "cliente"), pk=alocacao_id
    )
    if alocacao.status != Alocacao.Status.ATIVA:
        messages.warning(request, "Esta alocação já está encerrada.")
        return redirect("alocacoes:lista")
    aviso_km = None
    if request.method == "POST":
        try:
            data_termino, km_devolucao = _data_e_km_da_devolucao(request.POST, "data_termino")
            aviso_km = suspeita_de_km(
                km_devolucao, data_termino, alocacao.km_entrega, alocacao.data_inicio, "na entrega"
            )
            if aviso_km and not request.POST.get("confirmar_km"):
                raise ValidationError(aviso_km)
            alocacao.encerrar(data_termino=data_termino, km_devolucao=km_devolucao)
            messages.success(
                request, f"Alocação encerrada — {alocacao.veiculo.placa} está disponível."
            )
            _avisar_acerto_das_semanas(request, alocacao)
            # Encerrar dispara o acerto de caução quando há saldo retido (docs.md §4.2/§4.4).
            caucao = _caucao_com_saldo(alocacao)
            if caucao and caucao.saldo > 0:
                messages.warning(
                    request,
                    f"Faça o acerto da caução: {brl(caucao.saldo)} retidos "
                    f"de {alocacao.cliente.nome}.",
                )
                return redirect("financeiro:caucao_detalhe", caucao.pk)
            return redirect("alocacoes:lista")
        except (ValidationError, KeyError, ValueError) as erro:
            _avisar_erro_de_devolucao(request, erro)
    situacao = situacao_da_devolucao(alocacao)
    devolucao = situacao["vistoria_devolucao"]
    # KM e data já anotados na vistoria de devolução entram prontos no formulário.
    km_sugerido = devolucao.km if devolucao and devolucao.km else ""
    data_sugerida = devolucao.data if devolucao else date.today()
    return render(
        request,
        "alocacoes/encerrar.html",
        {
            "alocacao": alocacao,
            "hoje": date.today(),
            "caucao": _caucao_com_saldo(alocacao),
            "troca_ativa": alocacao.troca_ativa,
            "km_sugerido": km_sugerido,
            "data_sugerida": data_sugerida,
            "aviso_km": aviso_km,
            **situacao,
        },
    )


def _avisar_acerto_das_semanas(request, alocacao):
    """D03: o que foi pago por semanas que o cliente não vai usar."""
    acerto = getattr(alocacao, "acerto_semanas_nao_usadas", None) or {}
    nome = alocacao.cliente.nome
    if acerto.get("credito"):
        messages.info(
            request,
            f"{brl(acerto['credito'])} pagos por semanas depois da devolução viraram crédito "
            f"de {nome}. Devolva no acerto da caução ou use num próximo pagamento.",
        )
    if acerto.get("caucao"):
        messages.info(
            request,
            f"{brl(acerto['caucao'])} descontados da caução por semanas depois da devolução "
            "voltaram para a caução.",
        )
    if acerto.get("mes_anterior"):
        messages.warning(
            request,
            f"{brl(acerto['mes_anterior'])} foram pagos em mês anterior por semanas depois da "
            "devolução. Esse valor não foi mexido (o DAS daquele mês pode já ter sido "
            f"declarado): decida com a Luciana se devolve a {nome}.",
        )


def troca_nova(request, alocacao_id):
    alocacao = get_object_or_404(
        Alocacao.objects.select_related("veiculo", "cliente"), pk=alocacao_id
    )
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
            return _ficha(alocacao)
        except ValidationError as erro:
            for mensagens in erro.message_dict.values():
                for mensagem in mensagens:
                    form.add_error(None, mensagem)
    return render(request, "alocacoes/troca_nova.html", {"alocacao": alocacao, "form": form})


def troca_devolver(request, troca_id):
    troca = get_object_or_404(
        TrocaTemporaria.objects.select_related(
            "veiculo_substituto", "alocacao__veiculo", "alocacao__cliente"
        ),
        pk=troca_id,
    )
    if troca.data_devolucao:
        messages.warning(
            request, f"O substituto {troca.veiculo_substituto.placa} já foi devolvido."
        )
        return redirect("alocacoes:lista")
    aviso_km = None
    if request.method == "POST":
        try:
            data_devolucao, km_devolucao = _data_e_km_da_devolucao(request.POST, "data_devolucao")
            aviso_km = suspeita_de_km(
                km_devolucao, data_devolucao, troca.km_retirada, troca.data_retirada, "na retirada"
            )
            if aviso_km and not request.POST.get("confirmar_km"):
                raise ValidationError(aviso_km)
            troca.devolver(data_devolucao=data_devolucao, km_devolucao=km_devolucao)
            messages.success(request, f"Substituto {troca.veiculo_substituto.placa} devolvido.")
            return _ficha(troca.alocacao)
        except (ValidationError, KeyError, ValueError) as erro:
            _avisar_erro_de_devolucao(request, erro)
    return render(
        request,
        "alocacoes/troca_devolver.html",
        {"troca": troca, "hoje": date.today(), "aviso_km": aviso_km},
    )


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
    alocacao = get_object_or_404(
        Alocacao.objects.select_related("veiculo", "cliente"), pk=alocacao_id
    )
    if alocacao.contrato_aberto_em is None:
        # marca o passo "Imprimir o contrato" do cartão de próximos passos da entrega
        alocacao.contrato_aberto_em = date.today()
        alocacao.save(update_fields=["contrato_aberto_em"])
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


def vistoria_imprimir(request, alocacao_id):
    """Checklist de vistoria em branco para imprimir e preencher à mão."""
    alocacao = get_object_or_404(
        Alocacao.objects.select_related("veiculo", "cliente"), pk=alocacao_id
    )
    # D10: durante a troca o carro na frente do cliente é o substituto
    possiveis = Vistoria(alocacao=alocacao).veiculos_possiveis()
    escolhido = request.GET.get("veiculo", "")
    veiculo = possiveis.filter(pk=escolhido).first() if escolhido.isdigit() else None
    if veiculo is None:
        troca = alocacao.troca_ativa
        veiculo = troca.veiculo_substituto if troca else alocacao.veiculo
    return render(
        request,
        "alocacoes/vistoria_imprimir.html",
        {
            "alocacao": alocacao,
            "veiculo": veiculo,
            "itens": checklist.ITENS_CHECKLIST,
            "hoje": date.today(),
            "combustiveis": Vistoria.Combustivel.choices,
        },
    )


def vistoria_nova(request, alocacao_id):
    """Registra a vistoria — a foto do checklist preenchido carrega os campos."""
    alocacao = get_object_or_404(
        Alocacao.objects.select_related("veiculo", "cliente"), pk=alocacao_id
    )
    inicial = {"data": date.today()}
    if request.GET.get("tipo") in Vistoria.Tipo.values:
        # vindo dos passos da entrega/devolução, o tipo já chega escolhido
        inicial["tipo"] = request.GET["tipo"]
    form = VistoriaForm(
        request.POST or None, request.FILES or None, initial=inicial, alocacao=alocacao
    )
    if request.method == "POST" and form.is_valid():
        vistoria = form.save(commit=False)
        vistoria.alocacao = alocacao
        vistoria.save()
        messages.success(
            request,
            f"Vistoria de {vistoria.get_tipo_display().lower()} registrada "
            f"para {vistoria.veiculo.placa}.",
        )
        # Vindo dos passos da entrega (ficha) ou da devolução, volta para lá.
        voltar = request.GET.get("voltar", "")
        if voltar and url_has_allowed_host_and_scheme(
            voltar, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        ):
            return redirect(voltar)
        return redirect("alocacoes:timeline", vistoria.veiculo_id)
    return render(
        request,
        "alocacoes/vistoria_form.html",
        {
            "alocacao": alocacao,
            "form": form,
            "checklist_leitura": checklist.disponivel(),
            "vistorias": alocacao.vistorias.select_related("veiculo"),
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
