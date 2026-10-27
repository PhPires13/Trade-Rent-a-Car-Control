from datetime import date, timedelta
from uuid import uuid4

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import Max
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme, urlencode

from apps.campos import FormularioBR, exibir_dinheiro
from apps.financeiro.models import Caucao
from apps.financeiro.services import gerar_cobrancas_semanais
from apps.financeiro.views import _envio_unico
from apps.formatos import brl, milhar
from apps.frota.models import Veiculo
from apps.manutencao.services import oficina_por_veiculo
from apps.pessoas.models import Cliente

from . import checklist
from .entrega import situacao_da_devolucao
from .km_aviso import base_da_vistoria, suspeita_de_km
from .models import KM_MAXIMO, Alocacao, TrocaTemporaria, Vistoria
from .services import linha_do_tempo


def dividas_por_cliente(clientes_ids=None):
    """{cliente_id: {...}} do que cada um deve (D17).

    "vencido": venceu antes de hoje (ou judicial) — é o que pede confirmação
    para alocar. "ate_hoje": mesma conta do Quem deve (vencimento até hoje).
    "a_vencer": ainda vai vencer (não é atraso).
    """
    from apps.financeiro.models import Cobranca  # import local evita ciclo de import

    cobrancas = Cobranca.objects.filter(status__in=Cobranca.STATUS_DEVIDOS)
    if clientes_ids is not None:
        cobrancas = cobrancas.filter(cliente_id__in=clientes_ids)
    hoje = date.today()
    dividas = {}
    for cobranca in Cobranca.com_quitacao_anotada(cobrancas):
        if cobranca.saldo <= 0:
            continue
        divida = dividas.setdefault(
            cobranca.cliente_id,
            {"total": 0, "judicial": 0, "vencido": 0, "ate_hoje": 0, "a_vencer": 0},
        )
        divida["total"] += cobranca.saldo
        if cobranca.vencimento <= hoje:
            divida["ate_hoje"] += cobranca.saldo
        else:
            divida["a_vencer"] += cobranca.saldo
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


def _data_do_post(dados, campo):
    """Data digitada no formulário ainda não validado (ou None)."""
    try:
        return date.fromisoformat(str(dados.get(campo, "")).strip())
    except ValueError:
        return None


def _int_do_post(dados, campo):
    texto = str(dados.get(campo, "")).strip().replace(".", "")
    return int(texto) if texto.isdigit() else None


def semanas_a_lancar(inicio, hoje=None):
    """Quantas semanas de aluguel nascem de uma vez para um contrato que começou em `inicio`."""
    hoje = hoje or date.today()
    if inicio is None or inicio > hoje:
        return 0
    return (hoje - inicio).days // 7 + 1


def alocacao_ativa_do_cliente(cliente_id):
    return (
        Alocacao.objects.filter(cliente_id=cliente_id, status=Alocacao.Status.ATIVA)
        .select_related("veiculo", "cliente")
        .order_by("-data_inicio", "-pk")
        .first()
    )


def texto_cliente_com_carro(ativa):
    nome = ativa.cliente.nome.split()[0] if ativa.cliente.nome.strip() else ativa.cliente.nome
    return (
        f"{nome} já está com o carro {ativa.veiculo.placa} desde "
        f"{ativa.data_inicio:%d/%m/%Y}. Se trocou de carro, encerre a alocação anterior "
        "ou use Troca temporária."
    )


def texto_km_menor(km_atual):
    return (
        f"O carro já marcou {milhar(km_atual)} km — confira o painel. Se o KM do "
        "sistema estava errado, marque “Confirmo o KM” e salve de novo."
    )


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
    #: Data mais antiga que isso é quase sempre ano digitado errado.
    DIAS_PASSADO_MAXIMO = 366
    #: Acima disso, lançar as semanas atrasadas de uma vez pede confirmação.
    SEMANAS_SEM_CONFIRMAR = 26

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        hoje = date.today()
        self.fields["data_inicio"].widget.attrs["max"] = hoje.isoformat()
        self.fields["veiculo"].queryset = Veiculo.objects.filter(
            status=Veiculo.Status.DISPONIVEL, uso=Veiculo.Uso.LOCACAO
        )
        self.fields["cliente"].queryset = Cliente.objects.exclude(status=Cliente.Status.INATIVO)
        self.fields["veiculo"].empty_label = "Escolha o carro…"
        self.fields["cliente"].empty_label = "Escolha o cliente…"
        # Na lista de clientes: quem já está com carro e quem deve (vencido)
        self.carro_do_cliente = {
            a.cliente_id: a.veiculo.placa
            for a in Alocacao.objects.filter(status=Alocacao.Status.ATIVA).select_related("veiculo")
        }
        self.dividas = dividas_por_cliente()

        def rotulo_cliente(cliente):
            texto = cliente.nome
            if cliente.pk in self.carro_do_cliente:
                texto += f" — já com {self.carro_do_cliente[cliente.pk]}"
            vencido = (self.dividas.get(cliente.pk) or {}).get("ate_hoje")
            if vencido:
                texto += f" — deve {brl(vencido)}"
            return texto

        self.fields["cliente"].label_from_instance = rotulo_cliente
        dia = self.fields["dia_vencimento"]
        dia.required = False
        dia.choices = [("", "Mesmo dia da semana do início"), *Cliente.DiaSemana.choices]
        # D17: cliente devendo pode alugar, mas só com confirmação explícita
        self.texto_divida = None
        self.texto_dois_carros = None
        self.texto_km = None
        self.semanas = 0
        cliente_id = str(self.data.get("cliente", "")) if self.is_bound else ""
        cliente = Cliente.objects.filter(pk=cliente_id).first() if cliente_id.isdigit() else None
        if cliente:
            self.texto_divida = texto_da_divida(cliente, self.dividas.get(cliente.pk))
            ativa = alocacao_ativa_do_cliente(cliente.pk)
            if ativa:
                self.texto_dois_carros = texto_cliente_com_carro(ativa)
        if self.texto_divida:
            self.fields["confirmar_divida"] = forms.BooleanField(
                required=False, label="Confirmo: alocar mesmo com o cliente devendo"
            )
        if self.texto_dois_carros:
            self.fields["confirmar_dois_carros"] = forms.BooleanField(
                required=False, label="Confirmo: o cliente vai ficar com os dois carros"
            )
        if self.is_bound:
            # KM menor que o já conhecido (D27): pede confirmação
            veiculo_id = str(self.data.get("veiculo", ""))
            km = _int_do_post(self.data, "km_entrega")
            veiculo = (
                Veiculo.objects.filter(pk=veiculo_id).only("km_atual").first()
                if veiculo_id.isdigit()
                else None
            )
            if veiculo and km is not None and km < veiculo.km_atual:
                self.texto_km = texto_km_menor(veiculo.km_atual)
                self.fields["confirmar_km"] = forms.BooleanField(
                    required=False, label="Confirmo o KM"
                )
            # D01: muitas semanas atrasadas de uma vez só com confirmação
            inicio = _data_do_post(self.data, "cobrar_a_partir_de") or _data_do_post(
                self.data, "data_inicio"
            )
            self.semanas = semanas_a_lancar(inicio, hoje)
            if self.semanas > self.SEMANAS_SEM_CONFIRMAR:
                self.fields["confirmar_semanas"] = forms.BooleanField(
                    required=False,
                    label=f"Confirmo lançar {self.semanas} semanas atrasadas",
                )

    def clean_confirmar_divida(self):
        confirmado = self.cleaned_data.get("confirmar_divida")
        if self.texto_divida and not confirmado:
            raise ValidationError(
                f"{self.texto_divida} Para entregar o carro mesmo assim, marque a confirmação."
            )
        return confirmado

    def clean_confirmar_dois_carros(self):
        confirmado = self.cleaned_data.get("confirmar_dois_carros")
        if self.texto_dois_carros and not confirmado:
            raise ValidationError(
                f"{self.texto_dois_carros} Para alocar mais um carro, marque a confirmação."
            )
        return confirmado

    def clean_confirmar_km(self):
        confirmado = self.cleaned_data.get("confirmar_km")
        if self.texto_km and not confirmado:
            raise ValidationError(self.texto_km)
        return confirmado

    def _data_nao_antiga_demais(self, campo):
        valor = self.cleaned_data.get(campo)
        if valor and valor < date.today() - timedelta(days=self.DIAS_PASSADO_MAXIMO):
            raise ValidationError(f"Confira o ano — {valor:%d/%m/%Y} está longe demais.")
        return valor

    def clean_data_inicio(self):
        """Início antigo de verdade (contrato de anos, carga inicial) passa quando
        "Cobrar aluguel a partir de" está preenchido com data recente: aí só o
        histórico usa o início e nenhuma semana antiga é lançada."""
        valor = self.cleaned_data.get("data_inicio")
        limite = date.today() - timedelta(days=self.DIAS_PASSADO_MAXIMO)
        cobrar = _data_do_post(self.data, "cobrar_a_partir_de")
        if valor and valor < limite and not (cobrar and cobrar >= limite):
            raise ValidationError(
                f"Confira o ano — {valor:%d/%m/%Y} está longe demais. Se o contrato começou "
                "mesmo nessa data, preencha também “Cobrar aluguel a partir de” (em "
                "“Contrato que já rodava antes do sistema?”)."
            )
        return valor

    def clean_cobrar_a_partir_de(self):
        return self._data_nao_antiga_demais("cobrar_a_partir_de")

    def clean_dia_vencimento(self):
        valor = self.cleaned_data.get("dia_vencimento")
        return None if valor in (None, "") else valor

    def clean(self):
        """D01: contrato antigo precisa dizer de quando o sistema passa a cobrar.

        Sem isso, cadastrar um contrato de março lança de uma vez todas as
        semanas desde março, todas atrasadas, e o motorista vira inadimplente.
        """
        dados = super().clean()
        hoje = date.today()
        inicio = dados.get("data_inicio")
        antigo = inicio and inicio < hoje - timedelta(days=self.DIAS_INICIO_ANTIGO)
        if (
            antigo
            and not dados.get("cobrar_a_partir_de")
            and "cobrar_a_partir_de" not in self.errors
        ):
            self.add_error(
                "cobrar_a_partir_de",
                f"Este contrato começou em {inicio:%d/%m/%Y}, há mais de uma semana. "
                "Diga a partir de que dia o sistema deve cobrar o aluguel — as semanas "
                "antes disso não serão lançadas. Para cobrar tudo desde o início, "
                "repita aqui a data de início.",
            )
        cobrar = dados.get("cobrar_a_partir_de")
        # a data de início nunca é futura (Alocacao.clean): a conta parte de hoje,
        # sem somar dias a uma data digitada (9999 dava erro 500)
        if cobrar and cobrar > hoje + timedelta(days=self.DIAS_INICIO_ANTIGO):
            # ano digitado errado (2027) deixaria o carro rodando meses sem cobrança
            self.add_error(
                "cobrar_a_partir_de",
                f"{cobrar:%d/%m/%Y} está longe demais — confira a data. As semanas até "
                "lá não seriam cobradas.",
            )
        datas_ok = not {"data_inicio", "cobrar_a_partir_de"} & set(self.errors)
        if (
            datas_ok
            and self.semanas > self.SEMANAS_SEM_CONFIRMAR
            and not dados.get("confirmar_semanas")
        ):
            self.add_error(
                "confirmar_semanas",
                f"Serão lançadas {self.semanas} semanas de aluguel, todas atrasadas. Se o "
                "contrato já rodava antes do sistema, use “Cobrar aluguel a partir de” para "
                "lançar só as semanas que ainda não foram acertadas. Se é isso mesmo, "
                "marque a confirmação.",
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields[
            "valor_semanal"
        ].help_text = "Vale para as próximas semanas e para a semana em aberto sem pagamento."
        dia = self.fields["dia_vencimento"]
        dia.choices = list(Cliente.DiaSemana.choices)  # sem a opção vazia "---------"
        dia.help_text = (
            "Mudar o dia vale a partir da próxima cobrança: a primeira no dia novo sai "
            "proporcional, só pelos dias ainda não cobrados."
        )

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

    def __init__(self, *args, alocacao=None, **kwargs):
        super().__init__(*args, **kwargs)
        campo = self.fields["veiculo_substituto"]
        campo.queryset = Veiculo.objects.filter(
            status=Veiculo.Status.DISPONIVEL, uso=Veiculo.Uso.LOCACAO
        )
        campo.empty_label = "Escolha o carro substituto…"
        # Carro que ainda consta na oficina aparece marcado na lista de escolha.
        self.na_oficina = oficina_por_veiculo(list(campo.queryset.values_list("pk", flat=True)))
        campo.label_from_instance = lambda v: (
            f"{v.placa} — {v.marca_modelo}"
            + (
                f" ⚠ na oficina desde {self.na_oficina[v.pk].data_entrada:%d/%m}"
                if v.pk in self.na_oficina
                else ""
            )
        )
        if alocacao is not None:
            # as regras da troca (data, alocação ativa) já valem ao validar o formulário
            self.instance.alocacao = alocacao
        self.fields["km_retirada"].label = "KM do substituto ao sair"
        self.fields["data_retirada"].widget.attrs["max"] = date.today().isoformat()
        if alocacao is not None:
            self.fields["data_retirada"].widget.attrs["min"] = alocacao.data_inicio.isoformat()
        # KM menor que o já conhecido do substituto (D27): pede confirmação
        self.texto_km = None
        if self.is_bound:
            veiculo_id = str(self.data.get("veiculo_substituto", ""))
            km = _int_do_post(self.data, "km_retirada")
            veiculo = (
                Veiculo.objects.filter(pk=veiculo_id).only("km_atual").first()
                if veiculo_id.isdigit()
                else None
            )
            if veiculo and km is not None and km < veiculo.km_atual:
                self.texto_km = texto_km_menor(veiculo.km_atual)
                self.fields["confirmar_km"] = forms.BooleanField(
                    required=False, label="Confirmo o KM"
                )
        if alocacao is not None:
            self.fields["valor_semanal_ajustado"].help_text = (
                f"Hoje a semana custa {brl(alocacao.valor_semanal)}. Deixe vazio para manter "
                "esse valor; preencha só se o substituto tiver outro preço por semana."
            )

    def clean_confirmar_km(self):
        confirmado = self.cleaned_data.get("confirmar_km")
        if self.texto_km and not confirmado:
            raise ValidationError(self.texto_km)
        return confirmado


def _semanas_reajustadas(request, alteradas):
    """Diz na tela o novo valor das semanas em aberto refeitas pela troca (D02)."""
    for cobranca in alteradas or []:
        messages.info(
            request,
            f"A semana com vencimento em {cobranca.vencimento:%d/%m/%Y} passou a "
            f"{brl(cobranca.valor)} (dias divididos entre os dois carros).",
        )


def _voltar(request):
    """Endereço seguro de ?voltar= (ex.: a ficha do cliente), ou None."""
    voltar = request.GET.get("voltar", "")
    # só caminho deste site ("/clientes/10/"), nunca outro endereço
    if (
        voltar.startswith("/")
        and not voltar.startswith("//")
        and not voltar.startswith("/\\")
        and url_has_allowed_host_and_scheme(
            voltar, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        )
    ):
        return voltar
    return None


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
        self.fields["data"].widget.attrs["min"] = alocacao.data_inicio.isoformat()
        self.fields["data"].widget.attrs["max"] = date.today().isoformat()

    def clean_veiculo(self):
        veiculo = self.cleaned_data.get("veiculo")
        if veiculo is None:
            troca = self.alocacao.troca_ativa
            veiculo = troca.veiculo_substituto if troca else self.alocacao.veiculo
        return veiculo

    def _inicio_do_carro(self, veiculo):
        """Desde quando o motorista está com este carro nesta alocação."""
        if veiculo.pk != self.alocacao.veiculo_id:
            retirada = (
                self.alocacao.trocas.filter(veiculo_substituto=veiculo)
                .order_by("data_retirada")
                .values_list("data_retirada", flat=True)
                .first()
            )
            if retirada:
                return retirada, "a retirada do substituto"
        return self.alocacao.data_inicio, "o início da locação"

    def clean(self):
        dados = super().clean()
        veiculo, km, data = dados.get("veiculo"), dados.get("km"), dados.get("data")
        if veiculo and data:
            if data > date.today():
                self.add_error("data", "A data da vistoria não pode ser no futuro.")
                data = None
            else:
                inicio, rotulo_inicio = self._inicio_do_carro(veiculo)
                if data < inicio:
                    self.add_error(
                        "data",
                        f"A vistoria não pode ser antes de {rotulo_inicio} ({inicio:%d/%m/%Y}).",
                    )
                    data = None
        if veiculo and km and data:
            km_base, data_base, rotulo = base_da_vistoria(self.alocacao, veiculo)
            # KM menor que o já conhecido: entrega >= KM da entrega; devolução >= o
            # maior entre o da entrega e o atual do carro (esse KM preenche o encerramento)
            minimo = km_base
            if dados.get("tipo") == Vistoria.Tipo.SAIDA:
                minimo = max(km_base, veiculo.km_atual)
            if km < minimo:
                aviso = texto_km_menor(minimo)
            else:
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


def _maior_km_de_vistoria(alocacao, veiculo_id, desde):
    """Maior KM anotado em vistoria deste carro nesta alocação a partir de `desde`."""
    return alocacao.vistorias.filter(
        veiculo_id=veiculo_id, data__gte=desde, km__isnull=False
    ).aggregate(m=Max("km"))["m"]


def _aviso_km_abaixo_da_vistoria(km, km_vistoria):
    if km_vistoria and km < km_vistoria:
        return (
            f"Uma vistoria deste período já anotou {milhar(km_vistoria)} km — o KM não pode "
            f"voltar para {milhar(km)}. Confira o painel. Se a vistoria é que estava errada, "
            "marque “Confirmo o KM” e salve de novo."
        )
    return None


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
    alocacoes = list(alocacoes)
    # selo "deve R$ X" + botão "Recebi" nas ativas — uma consulta para todos
    dividas = dividas_por_cliente(
        {a.cliente_id for a in alocacoes if a.status == Alocacao.Status.ATIVA}
    )
    for alocacao in alocacoes:
        alocacao.divida = (
            dividas.get(alocacao.cliente_id) if alocacao.status == Alocacao.Status.ATIVA else None
        )
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
    dividas = form.dividas
    ativas = {
        a.cliente_id: a
        for a in Alocacao.objects.filter(status=Alocacao.Status.ATIVA)
        .select_related("veiculo", "cliente")
        .order_by("data_inicio", "pk")
    }
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
        if cliente.pk in ativas:
            # aviso de cliente que já está com carro (pede confirmação ao salvar)
            dados = {
                "dia": "",
                "dia_nome": "",
                "caucao": "",
                **dados,
                "com_carro": texto_cliente_com_carro(ativas[cliente.pk]),
            }
        if dados:
            clientes[str(cliente.pk)] = dados
    return veiculos, clientes


class _FormularioComErro(Exception):
    """Sai do envio único sem gravar o token: a pessoa corrige e envia de novo."""


def nova(request):
    inicial = {"data_inicio": date.today()}
    veiculo_id = request.GET.get("veiculo", "")
    if veiculo_id.isdigit():
        inicial["veiculo"] = veiculo_id
        veiculo = Veiculo.objects.filter(pk=veiculo_id).select_related("categoria").first()
        if veiculo:
            inicial["km_entrega"] = veiculo.km_atual
            if veiculo.categoria and veiculo.categoria.valor_semanal_referencia:
                # preço da categoria como sugestão — a pessoa pode mudar
                inicial["valor_semanal"] = veiculo.categoria.valor_semanal_referencia
    cliente_id = request.GET.get("cliente", "")
    if cliente_id.isdigit():
        inicial["cliente"] = cliente_id
        cliente = Cliente.objects.filter(pk=cliente_id).first()
        if cliente:
            # sugestão do cadastro — a pessoa ainda pode mudar na tela
            inicial["dia_vencimento"] = cliente.dia_vencimento
            inicial["caucao_valor"] = cliente.caucao_referencia or None
    form = AlocacaoForm(request.POST or None, initial=inicial)
    if request.method == "POST":
        token = request.POST.get("token", "")
        try:
            with _envio_unico(request, "alocacao_tokens_usados", token) as repetido:
                veiculo_post = str(request.POST.get("veiculo", ""))
                cliente_post = str(request.POST.get("cliente", ""))
                if (
                    repetido
                    and not Alocacao.objects.filter(
                        veiculo_id=veiculo_post if veiculo_post.isdigit() else None,
                        cliente_id=cliente_post if cliente_post.isdigit() else None,
                        status=Alocacao.Status.ATIVA,
                    ).exists()
                ):
                    # voltou com o botão "Voltar" e trocou carro/cliente: nada foi
                    # criado com estes dados — confere e envia de novo (token novo)
                    messages.warning(
                        request,
                        "Este formulário já tinha sido usado. Confira os dados e toque em "
                        "“Alocar carro” de novo.",
                    )
                    raise _FormularioComErro
                if repetido:
                    # toque duplo / reenvio: a alocação já foi criada pelo 1º envio
                    messages.info(
                        request,
                        "Esta alocação já tinha sido registrada — nada foi lançado em dobro.",
                    )
                    cliente_id = str(request.POST.get("cliente", ""))
                    if cliente_id.isdigit():
                        return redirect("pessoas:detalhe", cliente_id)
                    return redirect("alocacoes:lista")
                if not form.is_valid():
                    raise _FormularioComErro
                alocacao = form.save()  # sem dia de vencimento, o save usa o dia do início
                # Já cria a cobrança da 1ª semana (a rotina diária faria o mesmo amanhã):
                # o cartão "Próximos passos" mostra o valor e o botão de receber na hora.
                gerar_cobrancas_semanais()
        except _FormularioComErro:
            pass
        except IntegrityError:
            # dois envios ao mesmo tempo: o outro já alocou este carro
            messages.info(
                request, "Esta alocação já tinha sido registrada — nada foi lançado em dobro."
            )
            return redirect("alocacoes:lista")
        else:
            return _depois_de_alocar(request, alocacao)
    veiculos, clientes = _sugestoes_da_nova_alocacao(form)
    return render(
        request,
        "alocacoes/nova.html",
        {
            "form": form,
            "token": uuid4().hex,
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
            # contrato antigo: o campo "cobrar a partir de" já abre visível
            "abrir_contrato_antigo": bool(
                form["cobrar_a_partir_de"].value() or form.errors.get("cobrar_a_partir_de")
            ),
            "dias_inicio_antigo": AlocacaoForm.DIAS_INICIO_ANTIGO,
        },
    )


def _depois_de_alocar(request, alocacao):
    """Avisos (oficina, CNH vencida) e volta para a ficha com os próximos passos."""
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
            f"Atenção: a CNH de {cliente.nome} está vencida desde {cliente.cnh_validade:%d/%m/%Y}.",
        )
    messages.success(
        request,
        f"Veículo {alocacao.veiculo.placa} alocado a {cliente.nome}. "
        "Siga os próximos passos da entrega logo abaixo.",
    )
    return redirect("pessoas:detalhe", cliente.pk)


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
        if "valor_semanal" in form.changed_data:
            # valor corrigido: a semana em aberto (sem pagamento) passa ao valor novo
            from apps.financeiro.services import refazer_alugueis_em_aberto

            for cobranca in refazer_alugueis_em_aberto(alocacao) or []:
                messages.info(
                    request,
                    f"A semana de {cobranca.vencimento:%d/%m} em aberto foi refeita: "
                    f"{brl(cobranca.valor)}.",
                )
        messages.success(
            request,
            f"Alocação de {alocacao.veiculo.placa} → {alocacao.cliente.nome} atualizada.",
        )
        return _ficha(alocacao)
    return render(
        request,
        "alocacoes/editar.html",
        {
            "alocacao": alocacao,
            "form": form,
            "voltar": _voltar(request) or reverse("alocacoes:lista"),
        },
    )


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
            aviso_km = _aviso_km_abaixo_da_vistoria(
                km_devolucao,
                _maior_km_de_vistoria(alocacao, alocacao.veiculo_id, alocacao.data_inicio),
            ) or suspeita_de_km(
                km_devolucao, data_termino, alocacao.km_entrega, alocacao.data_inicio, "na entrega"
            )
            if aviso_km and not request.POST.get("confirmar_km"):
                raise ValidationError(aviso_km)
            alocacao.encerrar(data_termino=data_termino, km_devolucao=km_devolucao)
            messages.success(
                request,
                f"Alocação encerrada — {alocacao.veiculo.placa} está "
                f"{alocacao.veiculo.get_status_display().lower()}.",
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
            # a ficha do cliente mostra o resultado (crédito, caução, histórico)
            return _ficha(alocacao)
        except (ValidationError, KeyError, ValueError) as erro:
            _avisar_erro_de_devolucao(request, erro)
    situacao = situacao_da_devolucao(alocacao)
    if _voltar(request):
        # a vistoria volta para esta tela sem perder o ?voltar= (ficha do cliente)
        situacao["url_vistoria_devolucao"] = (
            reverse("alocacoes:vistoria_nova", args=[alocacao.pk])
            + "?"
            + urlencode({"tipo": "saida", "voltar": request.get_full_path()})
        )
    devolucao = situacao["vistoria_devolucao"]
    # KM e data já anotados na vistoria de devolução entram prontos no formulário
    # (só quando a vistoria é recente — situacao_da_devolucao decide: o KM de uma
    # vistoria de semanas atrás deixaria de cobrar o que o carro rodou depois).
    recente = situacao["data_da_vistoria"]
    km_sugerido = devolucao.km if recente and devolucao.km else ""
    ultima_troca = max(
        (t.data_devolucao for t in alocacao.trocas.all() if t.data_devolucao), default=None
    )
    data_minima = max(alocacao.data_inicio, ultima_troca or alocacao.data_inicio)
    return render(
        request,
        "alocacoes/encerrar.html",
        {
            "alocacao": alocacao,
            "hoje": date.today(),
            "caucao": _caucao_com_saldo(alocacao),
            "troca_ativa": alocacao.troca_ativa,
            "km_sugerido": km_sugerido,
            "data_minima": data_minima,
            "aviso_km": aviso_km,
            "voltar": _voltar(request) or reverse("alocacoes:lista"),
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
    form = TrocaForm(
        request.POST or None, initial={"data_retirada": date.today()}, alocacao=alocacao
    )
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
            oficina = form.na_oficina.get(troca.veiculo_substituto_id)
            if oficina:
                messages.warning(
                    request,
                    f"Atenção: {troca.veiculo_substituto.placa} consta como na oficina desde "
                    f"{oficina.data_entrada:%d/%m/%Y}. Se já voltou, marque "
                    "“Carro voltou da oficina” no histórico de manutenção.",
                )
            _semanas_reajustadas(request, getattr(troca, "semanas_reajustadas", None))
            return _ficha(alocacao)
        except ValidationError as erro:
            for mensagens in erro.message_dict.values():
                for mensagem in mensagens:
                    form.add_error(None, mensagem)
    candidatos = form.fields["veiculo_substituto"].queryset
    return render(
        request,
        "alocacoes/troca_nova.html",
        {
            "alocacao": alocacao,
            "form": form,
            # KM de cada candidato: preenche "KM do substituto ao sair" ao escolher
            "km_candidatos": {str(v.pk): v.km_atual for v in candidatos},
            "sem_candidatos": not candidatos.exists(),
            "voltar": _voltar(request) or reverse("alocacoes:lista"),
        },
    )


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
    # o cliente volta para o carro dele: avisa se ele ainda consta na oficina
    principal = troca.alocacao.veiculo
    oficina_principal = oficina_por_veiculo([principal.pk]).get(principal.pk)
    if request.method == "POST":
        try:
            data_devolucao, km_devolucao = _data_e_km_da_devolucao(request.POST, "data_devolucao")
            aviso_km = _aviso_km_abaixo_da_vistoria(
                km_devolucao,
                _maior_km_de_vistoria(
                    troca.alocacao, troca.veiculo_substituto_id, troca.data_retirada
                ),
            ) or suspeita_de_km(
                km_devolucao, data_devolucao, troca.km_retirada, troca.data_retirada, "na retirada"
            )
            if aviso_km and not request.POST.get("confirmar_km"):
                raise ValidationError(aviso_km)
            if oficina_principal and not request.POST.get("confirmar_oficina"):
                raise ValidationError(
                    f"{principal.placa} ainda consta na oficina. Se ele já voltou, marque "
                    "“Carro voltou da oficina” ou a confirmação abaixo."
                )
            troca.devolver(data_devolucao=data_devolucao, km_devolucao=km_devolucao)
            substituto = troca.veiculo_substituto
            messages.success(
                request,
                f"Substituto {substituto.placa} devolvido — agora está "
                f"{substituto.get_status_display().lower()}.",
            )
            _semanas_reajustadas(request, getattr(troca, "semanas_reajustadas", None))
            return _ficha(troca.alocacao)
        except (ValidationError, KeyError, ValueError) as erro:
            _avisar_erro_de_devolucao(request, erro)
    return render(
        request,
        "alocacoes/troca_devolver.html",
        {
            "troca": troca,
            "hoje": date.today(),
            "aviso_km": aviso_km,
            "oficina_principal": oficina_principal,
            "voltar": _voltar(request) or reverse("alocacoes:lista"),
        },
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
            "voltar": _voltar(request) or reverse("alocacoes:lista"),
            "empresa_incompleta": not (settings.EMPRESA_CNPJ and settings.EMPRESA_ENDERECO),
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
            "checklist_leitura": checklist.disponivel(),
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
    troca = alocacao.troca_ativa
    vigente = troca.veiculo_substituto if troca else alocacao.veiculo
    if request.GET.get("tipo") in Vistoria.Tipo.values:
        # vindo dos passos da entrega/devolução, o tipo já chega escolhido
        inicial["tipo"] = request.GET["tipo"]
    else:
        inicial["tipo"] = _tipo_sugerido(alocacao, vigente)
    # KM sugerido: o da entrega (ou da retirada do substituto) na entrega; o atual
    # do carro na devolução — a pessoa confere no painel
    km_base = base_da_vistoria(alocacao, vigente)[0]
    inicial["km"] = (
        km_base if inicial["tipo"] == Vistoria.Tipo.ENTRADA else max(km_base, vigente.km_atual)
    )
    form = VistoriaForm(
        request.POST or None, request.FILES or None, initial=inicial, alocacao=alocacao
    )
    form.fields["km"].widget.attrs.setdefault(
        "placeholder", f"último conhecido: {milhar(vigente.km_atual)} km"
    )
    form.fields["km"].widget.attrs.setdefault("inputmode", "numeric")
    if request.method == "POST":
        try:
            # token único: reenviar o formulário não grava a vistoria duas vezes
            with _envio_unico(
                request, "vistoria_tokens_usados", request.POST.get("token", "")
            ) as repetido:
                if repetido:
                    messages.info(
                        request,
                        "Esta vistoria já tinha sido registrada — nada foi gravado em dobro.",
                    )
                    return redirect(_voltar(request) or reverse("alocacoes:lista"))
                if not form.is_valid():
                    raise _FormularioComErro
                vistoria = form.save(commit=False)
                vistoria.alocacao = alocacao
                vistoria.save()
        except _FormularioComErro:
            pass
        else:
            return _depois_da_vistoria(request, vistoria)
    return render(
        request,
        "alocacoes/vistoria_form.html",
        {
            "alocacao": alocacao,
            "form": form,
            "token": uuid4().hex,
            "km_sugerido": not form.is_bound,
            "voltar": _voltar(request) or reverse("alocacoes:lista"),
            "checklist_leitura": checklist.disponivel(),
            "vistorias": alocacao.vistorias.select_related("veiculo"),
        },
    )


def _tipo_sugerido(alocacao, vigente):
    """Entrega ou devolução, pela vistoria mais recente do carro que está com o motorista.

    Sem vistoria, ou a última foi devolução (ou o substituto foi devolvido
    depois dela — o motorista retomou o carro): a próxima é a entrega.
    """
    ultima = alocacao.vistorias.filter(veiculo=vigente).order_by("-data", "-pk").first()
    if ultima is None or ultima.tipo == Vistoria.Tipo.SAIDA:
        return Vistoria.Tipo.ENTRADA
    fim_troca = alocacao.trocas.aggregate(m=Max("data_devolucao"))["m"]
    if vigente.pk == alocacao.veiculo_id and fim_troca and fim_troca > ultima.data:
        return Vistoria.Tipo.ENTRADA
    return Vistoria.Tipo.SAIDA


def _depois_da_vistoria(request, vistoria):
    messages.success(
        request,
        f"Vistoria de {vistoria.get_tipo_display().lower()} registrada "
        f"para {vistoria.veiculo.placa}.",
    )
    # Vindo dos passos da entrega (ficha) ou da devolução, volta para lá.
    voltar = _voltar(request)
    if voltar:
        return redirect(voltar)
    return redirect("alocacoes:timeline", vistoria.veiculo_id)


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
