import json
from datetime import timedelta
from uuid import uuid4

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme, urlencode

from apps.campos import FormularioBR, exibir_dinheiro, id_valido, ler_data
from apps.financeiro.models import Caucao
from apps.financeiro.services import gerar_cobrancas_semanais
from apps.financeiro.views import _envio_unico
from apps.formatos import brl, milhar
from apps.frota.models import Veiculo
from apps.km.forms import CampoKm, km_digitado
from apps.manutencao.services import manutencoes_em_aberto, oficina_por_veiculo
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
    hoje = timezone.localdate()
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
    """Data digitada no formulário ainda não validado (ou None).

    Mesmos formatos do formulário (ler_data): "01/07/26" também conta, senão a
    confirmação de semanas atrasadas (D01) era pulada.
    """
    try:
        return ler_data(dados.get(campo, ""))
    except ValidationError:
        return None


def _int_do_post(dados, campo):
    """KM digitado no formulário ainda não validado ("35.000" = 35000), ou None."""
    try:
        return km_digitado(str(dados.get(campo, "")))
    except ValidationError:
        return None


def _km_com_milhar(form, nome):
    """Campo de KM aceita "35.000" (vira 35000, não 35) e abre o teclado numérico.

    O IntegerField padrão (e o type="number" do navegador) lê "35.000" como 35.
    """
    antigo = form.fields[nome]
    novo = CampoKm(
        label=antigo.label,
        required=antigo.required,
        help_text=antigo.help_text,
        widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "off"}),
    )
    novo.validators = list(antigo.validators)
    form.fields[nome] = novo


#: Franquia mensal abaixo disso é quase sempre "3.000" lido como 3 (ou dígito faltando).
FRANQUIA_MINIMA_KM = 500


def _franquia_razoavel(form):
    """Franquia de km por mês: "5.000" vira 5000 e número baixo demais é recusado.

    Com a franquia errada, a leitura mensal cobra sozinha quase todo o km rodado.
    """
    franquia = form.cleaned_data.get("franquia_km_mensal")
    if form.cleaned_data.get("limite_km") == Alocacao.LimiteKm.ILIMITADO:
        # sem limite o campo fica escondido na tela: um erro nele não apareceria
        return franquia
    if franquia is not None and franquia < FRANQUIA_MINIMA_KM:
        raise ValidationError(
            f"Franquia de {milhar(franquia)} km por mês é baixa demais — confira o número "
            f"(ex.: 3.000 ou 5000). O mínimo aceito é {milhar(FRANQUIA_MINIMA_KM)} km."
        )
    return franquia


def semanas_a_lancar(inicio, hoje=None):
    """Quantas semanas de aluguel nascem de uma vez para um contrato que começou em `inicio`."""
    hoje = hoje or timezone.localdate()
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
        hoje = timezone.localdate()
        self.fields["data_inicio"].widget.attrs["max"] = hoje.isoformat()
        _km_com_milhar(self, "km_entrega")
        _km_com_milhar(self, "franquia_km_mensal")
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
        cliente = Cliente.objects.filter(pk=cliente_id).first() if id_valido(cliente_id) else None
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
                if id_valido(veiculo_id)
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

    def clean_franquia_km_mensal(self):
        return _franquia_razoavel(self)

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
        if valor and valor < timezone.localdate() - timedelta(days=self.DIAS_PASSADO_MAXIMO):
            raise ValidationError(f"Confira o ano — {valor:%d/%m/%Y} está longe demais.")
        return valor

    def clean_data_inicio(self):
        """Início antigo de verdade (contrato de anos, carga inicial) passa quando
        "Cobrar aluguel a partir de" está preenchido com data recente: aí só o
        histórico usa o início e nenhuma semana antiga é lançada."""
        valor = self.cleaned_data.get("data_inicio")
        limite = timezone.localdate() - timedelta(days=self.DIAS_PASSADO_MAXIMO)
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
        hoje = timezone.localdate()
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
        _km_com_milhar(self, "franquia_km_mensal")
        self.fields[
            "valor_semanal"
        ].help_text = "Vale para as próximas semanas e para a semana em aberto sem pagamento."
        dia = self.fields["dia_vencimento"]
        dia.choices = list(Cliente.DiaSemana.choices)  # sem a opção vazia "---------"
        dia.help_text = (
            "Mudar o dia vale a partir da próxima cobrança: a primeira no dia novo sai "
            "proporcional, só pelos dias ainda não cobrados."
        )

    def clean_franquia_km_mensal(self):
        return _franquia_razoavel(self)

    #: Caução combinada menor que a já depositada: a tela pede esta confirmação.
    confirmar_caucao_menor = False

    def clean_caucao_valor(self):
        """Não some com a caução combinada enquanto houver dinheiro retido (C01)."""
        valor = self.cleaned_data.get("caucao_valor")
        if not self.instance.pk:
            return valor
        caucao = _caucao_com_saldo(self.instance)
        if not valor:
            if caucao and caucao.saldo > 0:
                raise ValidationError(
                    f"Ainda há {brl(caucao.saldo)} retidos na caução deste cliente. "
                    "Devolva ou desconte esse valor em Financeiro > Cauções antes de "
                    "tirar a caução do contrato."
                )
        elif caucao and caucao.saldo > valor and "caucao_valor" in self.changed_data:
            # o dinheiro a mais não sai sozinho da caução: só a tela da caução devolve.
            # A caixa continua na tela (marcada) se outro campo der erro.
            self.confirmar_caucao_menor = True
            if self.data.get("confirmar_caucao"):
                return valor
            retidos = (
                f" ({brl(caucao.saldo)} retidos hoje)" if caucao.saldo != caucao.recebido else ""
            )
            raise ValidationError(
                f"Já foram depositados {brl(caucao.recebido)}{retidos}. Os "
                f"{brl(caucao.saldo - valor)} a mais continuam guardados — devolva pela tela "
                "da caução se combinou devolver. Para salvar assim, marque a confirmação."
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
        _km_com_milhar(self, "km_retirada")
        self.fields["data_retirada"].widget.attrs["max"] = timezone.localdate().isoformat()
        if alocacao is not None:
            ultima_volta = alocacao.trocas.aggregate(m=Max("data_devolucao"))["m"]
            inicio = max(alocacao.data_inicio, ultima_volta or alocacao.data_inicio)
            self.fields["data_retirada"].widget.attrs["min"] = inicio.isoformat()
        # KM menor que o já conhecido do substituto (D27): pede confirmação
        self.texto_km = None
        if self.is_bound:
            veiculo_id = str(self.data.get("veiculo_substituto", ""))
            km = _int_do_post(self.data, "km_retirada")
            veiculo = (
                Veiculo.objects.filter(pk=veiculo_id).only("km_atual").first()
                if id_valido(veiculo_id)
                else None
            )
            if veiculo and km is not None and km < veiculo.km_atual:
                # erro junto do campo de KM, com o número digitado (C: a caixa de
                # confirmação ficava no fim do formulário, depois de Observações)
                self.texto_km = f"Você digitou {milhar(km)} km. " + texto_km_menor(veiculo.km_atual)
                self.fields["confirmar_km"] = forms.BooleanField(
                    required=False, label="Confirmo o KM"
                )
        if alocacao is not None:
            self.fields["valor_semanal_ajustado"].help_text = (
                f"Hoje a semana custa {brl(alocacao.valor_semanal)}. Deixe vazio para manter "
                "esse valor; preencha só se o substituto tiver outro preço por semana."
            )

    def clean(self):
        dados = super().clean()
        if self.texto_km and not dados.get("confirmar_km") and "km_retirada" not in self.errors:
            self.add_error("km_retirada", self.texto_km)
        return dados


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

    def __init__(self, *args, alocacao, so_o_carro_com_o_motorista=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.alocacao = alocacao
        self.instance.alocacao = alocacao
        _km_com_milhar(self, "km")
        # D10: só o carro da locação e os substitutos dela; na troca, o substituto vem marcado
        campo = self.fields["veiculo"]
        campo.queryset = self.instance.veiculos_possiveis()
        troca = alocacao.troca_ativa
        if so_o_carro_com_o_motorista:
            # devolução: só o carro que está com o motorista (um toque errado no
            # celular vistoriava o substituto já devolvido)
            campo.queryset = campo.queryset.filter(
                pk=troca.veiculo_substituto_id if troca else alocacao.veiculo_id
            )
        # sem escolha (tela antiga, leitura da foto) vale o carro sugerido
        campo.required = False
        campo.empty_label = None
        # substituto já devolvido: "(substituto até 12/11)"
        devolvidos = {}
        for antiga in alocacao.trocas.all():
            if antiga.data_devolucao:
                anterior = devolvidos.get(antiga.veiculo_substituto_id)
                devolvidos[antiga.veiculo_substituto_id] = max(
                    antiga.data_devolucao, anterior or antiga.data_devolucao
                )
        self.tem_troca_aberta = troca is not None

        def rotulo(veiculo):
            if veiculo.pk == alocacao.veiculo_id:
                papel = " (carro da locação)"
            elif troca and veiculo.pk == troca.veiculo_substituto_id:
                papel = " (substituto)"
            elif veiculo.pk in devolvidos:
                papel = f" (substituto até {devolvidos[veiculo.pk]:%d/%m})"
            else:
                papel = " (substituto)"
            return f"{veiculo.placa} — {veiculo.marca_modelo}{papel}"

        campo.label_from_instance = rotulo
        self.initial.setdefault(
            "veiculo", troca.veiculo_substituto_id if troca else alocacao.veiculo_id
        )
        # data mínima = desde quando o motorista está com o carro escolhido (no
        # substituto, a retirada); o script da tela troca o min ao mudar o carro
        inicios = {str(v.pk): self._inicio_do_carro(v)[0].isoformat() for v in campo.queryset}
        campo.widget.attrs["data-inicios"] = json.dumps(inicios)
        escolhido = self.data.get("veiculo") if self.is_bound else None
        escolhido = escolhido or self.initial.get("veiculo")
        self.fields["data"].widget.attrs["min"] = inicios.get(
            str(escolhido), alocacao.data_inicio.isoformat()
        )
        self.fields["data"].widget.attrs["max"] = timezone.localdate().isoformat()

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
                return retirada, "da retirada do substituto"
        return self.alocacao.data_inicio, "do início da locação"

    def clean(self):
        dados = super().clean()
        veiculo, km, data = dados.get("veiculo"), dados.get("km"), dados.get("data")
        if veiculo and data:
            if data > timezone.localdate():
                self.add_error("data", "A data da vistoria não pode ser no futuro.")
                data = None
            else:
                inicio, rotulo_inicio = self._inicio_do_carro(veiculo)
                if data < inicio:
                    self.add_error(
                        "data",
                        f"A vistoria não pode ser antes {rotulo_inicio} ({inicio:%d/%m/%Y}).",
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
    data = ler_data(post.get(campo_data))
    km = km_digitado(post.get("km_devolucao", ""))
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


def _aviso_km_do_substituto(troca, data, km):
    """(aviso, dá para corrigir a retirada) do KM do substituto na devolução (D27).

    Só o aviso de "km rodados demais" pode ter o erro na retirada; o de KM
    abaixo de uma vistoria é da própria devolução.
    """
    aviso = _aviso_km_abaixo_da_vistoria(
        km,
        _maior_km_de_vistoria(troca.alocacao, troca.veiculo_substituto_id, troca.data_retirada),
    )
    if aviso:
        return aviso, False
    aviso = suspeita_de_km(km, data, troca.km_retirada, troca.data_retirada, "na retirada")
    return aviso, aviso is not None


def _retirada_parece_errada(troca, data, km_devolucao):
    """A devolução bate com o KM que o substituto já tinha, a retirada não.

    Ex.: retirada digitada 14.314 (era 143.140) e devolução 143.200 — o erro
    está na retirada, não na devolução.
    """
    km_conhecido = troca.veiculo_substituto.km_atual
    return (
        troca.km_retirada < km_conhecido <= km_devolucao
        and suspeita_de_km(km_devolucao, data, troca.km_retirada, troca.data_retirada, "")
        is not None
        and suspeita_de_km(km_devolucao, data, km_conhecido, troca.data_retirada, "") is None
    )


def _km_confirmado_na_vistoria(situacao, km):
    """O KM da devolução é o mesmo da vistoria de devolução (já conferido lá)."""
    devolucao = situacao["vistoria_devolucao"]
    return bool(devolucao and devolucao.km and str(devolucao.km) == str(km))


def _corrigir_km_da_retirada(post, troca, km_devolucao):
    """Lê o "KM na retirada" corrigido na devolução; devolve o KM antigo (ou None).

    Só muda a troca em memória: o save da devolução grava junto (e no histórico).
    """
    texto = (post.get("km_retirada") or "").strip()
    if not texto:
        return None
    novo = km_digitado(texto)
    if novo == troca.km_retirada:
        return None
    if novo > km_devolucao:
        raise ValidationError(
            f"O KM na retirada ({milhar(novo)}) não pode ser maior que o da devolução "
            f"({milhar(km_devolucao)})."
        )
    antigo, troca.km_retirada = troca.km_retirada, novo
    return antigo


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
    hoje = timezone.localdate()
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
        if cliente.cnh_validade and cliente.cnh_validade < hoje:
            # aviso já ao escolher (só avisa — não pede confirmação)
            dados = {
                "dia": "",
                "dia_nome": "",
                "caucao": "",
                **dados,
                "cnh": f"CNH vencida desde {cliente.cnh_validade:%d/%m/%Y} — confira antes "
                "de entregar o carro.",
            }
        if dados:
            clientes[str(cliente.pk)] = dados
    return veiculos, clientes


class _FormularioComErro(Exception):
    """Sai do envio único sem gravar o token: a pessoa corrige e envia de novo."""


def nova(request):
    inicial = {"data_inicio": timezone.localdate()}
    veiculo_id = request.GET.get("veiculo", "")
    if id_valido(veiculo_id):
        inicial["veiculo"] = veiculo_id
        veiculo = Veiculo.objects.filter(pk=veiculo_id).select_related("categoria").first()
        if veiculo:
            inicial["km_entrega"] = veiculo.km_atual
            if veiculo.categoria and veiculo.categoria.valor_semanal_referencia:
                # preço da categoria como sugestão — a pessoa pode mudar
                inicial["valor_semanal"] = veiculo.categoria.valor_semanal_referencia
    cliente_id = request.GET.get("cliente", "")
    if id_valido(cliente_id):
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
                        veiculo_id=veiculo_post if id_valido(veiculo_post) else None,
                        cliente_id=cliente_post if id_valido(cliente_post) else None,
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
                    if id_valido(cliente_id):
                        return redirect("pessoas:detalhe", cliente_id)
                    return redirect("alocacoes:lista")
                if not form.is_valid():
                    raise _FormularioComErro
                alocacao = form.save()  # sem dia de vencimento, o save usa o dia do início
                # Já cria a cobrança da 1ª semana (a rotina diária faria o mesmo amanhã):
                # o cartão "Próximos passos" mostra o valor e o botão de receber na hora.
                # Só desta alocação: um contrato com dado ruim em outro carro não
                # pode barrar o cadastro (a rotina diária cuida da frota).
                gerar_cobrancas_semanais(alocacao=alocacao)
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
    if cliente.cnh_validade and cliente.cnh_validade < timezone.localdate():
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
        Alocacao.objects.select_related("veiculo", "cliente").prefetch_related(
            "trocas__veiculo_substituto"
        ),
        pk=alocacao_id,
    )
    if alocacao.status != Alocacao.Status.ATIVA:
        messages.warning(request, "Esta alocação já está encerrada.")
        return redirect("alocacoes:lista")
    aviso_km = None
    retirada_errada = corrigir_retirada = False
    # Com substituto: o cliente devolve o substituto de vez e o contrato acaba junto
    # (o carro dele pode seguir na oficina, sem cliente). O KM digitado é o do substituto.
    troca = alocacao.troca_ativa
    situacao = situacao_da_devolucao(alocacao)
    if request.method == "POST":
        try:
            data_termino, km_devolucao = _data_e_km_da_devolucao(request.POST, "data_termino")
            if troca and request.POST.get("troca") != str(troca.pk):
                raise ValidationError(
                    f"{alocacao.cliente.nome} está com o substituto "
                    f"{troca.veiculo_substituto.placa} — confira o KM dele e envie de novo."
                )
            if not troca and request.POST.get("troca"):
                raise ValidationError(
                    "O substituto já foi devolvido em outra tela — confira o KM do "
                    f"{alocacao.veiculo.placa} e envie de novo."
                )
            km_corrigido = (
                _corrigir_km_da_retirada(request.POST, troca, km_devolucao) if troca else None
            )
            if troca:
                aviso_km, corrigir_retirada = _aviso_km_do_substituto(
                    troca, data_termino, km_devolucao
                )
                retirada_errada = corrigir_retirada and _retirada_parece_errada(
                    troca, data_termino, km_devolucao
                )
            else:
                aviso_km = _aviso_km_abaixo_da_vistoria(
                    km_devolucao,
                    _maior_km_de_vistoria(alocacao, alocacao.veiculo_id, alocacao.data_inicio),
                ) or suspeita_de_km(
                    km_devolucao,
                    data_termino,
                    alocacao.km_entrega,
                    alocacao.data_inicio,
                    "na entrega",
                )
            if aviso_km and _km_confirmado_na_vistoria(situacao, km_devolucao):
                # o mesmo KM já foi conferido (e confirmado) na vistoria de devolução
                aviso_km = None
            if aviso_km and not request.POST.get("confirmar_km"):
                raise ValidationError(aviso_km)
            with transaction.atomic():
                if troca:
                    troca.devolver(data_devolucao=data_termino, km_devolucao=km_devolucao)
                    # o carro do contrato não voltou a rodar: fica com o KM que já tinha
                    principal = alocacao.veiculo
                    principal.refresh_from_db(fields=["km_atual", "status"])
                    alocacao.encerrar(
                        data_termino=data_termino,
                        km_devolucao=max(alocacao.km_entrega, principal.km_atual),
                    )
                else:
                    alocacao.encerrar(data_termino=data_termino, km_devolucao=km_devolucao)
            if troca:
                messages.success(
                    request,
                    f"Substituto {troca.veiculo_substituto.placa} devolvido e alocação "
                    f"encerrada — {alocacao.veiculo.placa} está "
                    f"{alocacao.veiculo.get_status_display().lower()}.",
                )
                if km_corrigido is not None:
                    messages.info(
                        request,
                        f"KM da retirada do {troca.veiculo_substituto.placa} corrigido de "
                        f"{milhar(km_corrigido)} para {milhar(troca.km_retirada)}.",
                    )
            else:
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
            # nada foi gravado: relê a troca e a alocação do banco para a tela
            alocacao.refresh_from_db()
            troca = alocacao.troca_ativa
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
    if troca:
        data_minima = max(data_minima, troca.data_retirada)
    return render(
        request,
        "alocacoes/encerrar.html",
        {
            "alocacao": alocacao,
            "hoje": timezone.localdate(),
            "caucao": _caucao_com_saldo(alocacao),
            "troca_ativa": troca,
            "oficina_principal": (
                oficina_por_veiculo([alocacao.veiculo_id]).get(alocacao.veiculo_id)
                if troca
                else None
            ),
            "km_sugerido": km_sugerido,
            # KM que o encerramento mostra como ponto de partida (o do substituto, se houver)
            "km_saida": troca.km_retirada if troca else alocacao.km_entrega,
            "data_minima": data_minima,
            "aviso_km": aviso_km,
            "retirada_errada": retirada_errada,
            "corrigir_retirada": corrigir_retirada,
            "km_confirmado_na_vistoria": bool(
                km_sugerido and devolucao and _km_confirmado_na_vistoria(situacao, km_sugerido)
            ),
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
    # clique duplo, Voltar ou duas abas: a troca já foi feita ou a locação acabou
    if alocacao.status != Alocacao.Status.ATIVA:
        messages.info(request, "Esta locação já foi encerrada — não dá mais para trocar o carro.")
        return _ficha(alocacao)
    em_andamento = alocacao.troca_ativa
    if em_andamento:
        messages.info(
            request,
            f"{alocacao.cliente.nome} já está com o substituto "
            f"{em_andamento.veiculo_substituto.placa}. Devolva-o antes de trocar de novo.",
        )
        return _ficha(alocacao)
    form = TrocaForm(
        request.POST or None, initial={"data_retirada": timezone.localdate()}, alocacao=alocacao
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
        except IntegrityError:
            # dois envios ao mesmo tempo (toque duplo, duas abas): o outro já gravou a
            # troca, ou o substituto foi para outro cliente nesse meio-tempo
            messages.info(
                request,
                "Esta troca já tinha sido registrada (ou o carro acabou de ser usado em outra "
                "troca) — confira a ficha do cliente.",
            )
            return _ficha(alocacao)
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


def _fechar_oficina(veiculo, data):
    """Fecha as idas à oficina em aberto do carro na data dada; devolve as datas de saída."""
    saidas = []
    for manutencao in manutencoes_em_aberto().filter(veiculo=veiculo):
        # a saída nunca fica antes da entrada (entrada lançada depois da retirada)
        manutencao.data_saida = max(data, manutencao.data_entrada)
        manutencao.save(update_fields=["data_saida"])
        saidas.append(manutencao.data_saida)
    return saidas


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
    retirada_errada = corrigir_retirada = False
    # o cliente volta para o carro dele: avisa se ele ainda consta na oficina
    principal = troca.alocacao.veiculo
    oficina_principal = oficina_por_veiculo([principal.pk]).get(principal.pk)
    if request.method == "POST":
        try:
            data_devolucao, km_devolucao = _data_e_km_da_devolucao(request.POST, "data_devolucao")
            km_corrigido = _corrigir_km_da_retirada(request.POST, troca, km_devolucao)
            aviso_km, corrigir_retirada = _aviso_km_do_substituto(
                troca, data_devolucao, km_devolucao
            )
            retirada_errada = corrigir_retirada and _retirada_parece_errada(
                troca, data_devolucao, km_devolucao
            )
            if aviso_km and not request.POST.get("confirmar_km"):
                raise ValidationError(aviso_km)
            if oficina_principal and not request.POST.get("confirmar_oficina"):
                raise ValidationError(
                    f"{principal.placa} ainda consta na oficina. Se ele já voltou, marque "
                    "“Carro voltou da oficina” ou a confirmação abaixo."
                )
            with transaction.atomic():
                troca.devolver(data_devolucao=data_devolucao, km_devolucao=km_devolucao)
                # confirmou que o cliente voltou para o carro dele: o carro saiu da
                # oficina nesta data — senão os dias parado (auxílio) seguiam contando
                saidas = _fechar_oficina(principal, data_devolucao) if oficina_principal else []
            substituto = troca.veiculo_substituto
            messages.success(
                request,
                f"Substituto {substituto.placa} devolvido — agora está "
                f"{substituto.get_status_display().lower()}.",
            )
            for saida in saidas:
                messages.info(request, f"{principal.placa} saiu da oficina em {saida:%d/%m/%Y}.")
            if km_corrigido is not None:
                messages.info(
                    request,
                    f"KM da retirada do {substituto.placa} corrigido de {milhar(km_corrigido)} "
                    f"para {milhar(troca.km_retirada)}.",
                )
            _semanas_reajustadas(request, getattr(troca, "semanas_reajustadas", None))
            return _ficha(troca.alocacao)
        except (ValidationError, KeyError, ValueError) as erro:
            _avisar_erro_de_devolucao(request, erro)
            troca.refresh_from_db()  # nada foi gravado (KM da retirada inclusive)
    return render(
        request,
        "alocacoes/troca_devolver.html",
        {
            "troca": troca,
            "hoje": timezone.localdate(),
            "aviso_km": aviso_km,
            "retirada_errada": retirada_errada,
            "corrigir_retirada": corrigir_retirada,
            "oficina_principal": oficina_principal,
            # cliente desistindo de vez: encerra o contrato devolvendo o substituto
            "url_encerrar": reverse("alocacoes:encerrar", args=[troca.alocacao_id])
            + "?"
            + urlencode({"voltar": reverse("pessoas:detalhe", args=[troca.alocacao.cliente_id])}),
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
    # pré-carregamento do navegador (link só "visto", não aberto) não conta como impresso
    pre_carga = (
        "prefetch"
        in (request.headers.get("Sec-Purpose", "") + request.headers.get("Purpose", "")).lower()
    )
    if alocacao.contrato_aberto_em is None and not pre_carga:
        # marca o passo "Imprimir o contrato" do cartão de próximos passos da entrega
        # (abrir o contrato é o gesto de imprimir: decisão de manter simples)
        alocacao.contrato_aberto_em = timezone.localdate()
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
            "hoje": timezone.localdate(),
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
    veiculo = possiveis.filter(pk=escolhido).first() if id_valido(escolhido) else None
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
            "hoje": timezone.localdate(),
            "combustiveis": Vistoria.Combustivel.choices,
        },
    )


def vistoria_nova(request, alocacao_id):
    """Registra a vistoria — a foto do checklist preenchido carrega os campos."""
    alocacao = get_object_or_404(
        Alocacao.objects.select_related("veiculo", "cliente"), pk=alocacao_id
    )
    inicial = {"data": timezone.localdate()}
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
        request.POST or None,
        request.FILES or None,
        initial=inicial,
        alocacao=alocacao,
        so_o_carro_com_o_motorista=request.GET.get("tipo") == Vistoria.Tipo.SAIDA,
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
                ajuste_km = _ajustar_km_de_partida(vistoria)
        except _FormularioComErro:
            pass
        else:
            if ajuste_km:
                messages.info(request, ajuste_km)
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


#: Vistoria de entrega até tantos dias depois da entrega/retirada ainda corrige o KM dela.
DIAS_AJUSTE_KM_PARTIDA = 7


def _ajustar_km_de_partida(vistoria):
    """Vistoria de entrega com o KM lido no painel corrige o KM anotado ao alocar.

    O contrato, a devolução e o excedente de km partem do KM da entrega (ou da
    retirada do substituto): o número que o Paulo leu no painel é o que vale.
    Só a 1ª vistoria de entrega daquele carro, logo depois da entrega, com o
    contrato (ou a troca) ainda em aberto e sem leitura mensal de KM depois
    (ela já usou o número antigo). Devolve o texto do aviso, ou None.
    """
    from apps.km.models import RegistroKm  # import local evita ciclo de import

    if vistoria.tipo != Vistoria.Tipo.ENTRADA or not vistoria.km:
        return None
    alocacao = vistoria.alocacao
    outras_entregas = alocacao.vistorias.filter(
        veiculo_id=vistoria.veiculo_id, tipo=Vistoria.Tipo.ENTRADA
    ).exclude(pk=vistoria.pk)
    limite = timedelta(days=DIAS_AJUSTE_KM_PARTIDA)
    if vistoria.veiculo_id == alocacao.veiculo_id:
        if (
            alocacao.status != Alocacao.Status.ATIVA
            or vistoria.km == alocacao.km_entrega
            or vistoria.data > alocacao.data_inicio + limite
            or outras_entregas.exists()
            or alocacao.trocas.filter(data_retirada__lte=vistoria.data).exists()
            or RegistroKm.objects.filter(
                veiculo_id=alocacao.veiculo_id, data_leitura__gte=alocacao.data_inicio
            ).exists()
        ):
            return None
        objeto, campo, veiculo = alocacao, "km_entrega", alocacao.veiculo
        rotulo = "KM de entrega do contrato"
    else:
        troca = (
            alocacao.trocas.filter(
                veiculo_substituto_id=vistoria.veiculo_id,
                data_retirada__lte=vistoria.data,
                data_devolucao__isnull=True,
            )
            .select_related("veiculo_substituto")
            .first()
        )
        if (
            troca is None
            or vistoria.km == troca.km_retirada
            or vistoria.data > troca.data_retirada + limite
            or outras_entregas.filter(data__gte=troca.data_retirada).exists()
            or RegistroKm.objects.filter(
                veiculo_id=troca.veiculo_substituto_id, data_leitura__gte=troca.data_retirada
            ).exists()
        ):
            return None
        objeto, campo, veiculo = troca, "km_retirada", troca.veiculo_substituto
        rotulo = f"KM da retirada do substituto {veiculo.placa}"
    antigo = getattr(objeto, campo)
    maior = _gravar_km_de_partida(objeto, campo, veiculo, vistoria.km)
    if maior is not None:
        # para baixo, mas outro lançamento do carro já passou do número da vistoria:
        # o carro não anda para trás — nada muda, a pessoa confere
        return (
            f"{rotulo} continua {milhar(antigo)}: a vistoria marcou {milhar(vistoria.km)} km, "
            f"mas o carro já tinha {milhar(maior)} km em outro lançamento — confira o painel."
        )
    return (
        f"{rotulo} ajustado para {milhar(vistoria.km)} (estava {milhar(antigo)}), como na vistoria."
    )


class _KmParaTras(Exception):
    pass


def _gravar_km_de_partida(objeto, campo, veiculo, novo):
    """Grava o KM de partida corrigido; devolve None, ou o KM que impediu a correção.

    Para baixo só vale se nenhum outro lançamento do carro passar do novo número;
    nesse caso o KM atual do carro também volta para o maior conhecido (D27).
    """
    from apps.km.correcao import km_atual_recalculado  # import local evita ciclo de import

    antigo = getattr(objeto, campo)
    try:
        with transaction.atomic():
            setattr(objeto, campo, novo)
            objeto.save(update_fields=[campo])
            if novo < antigo:
                maior = km_atual_recalculado(veiculo)
                if maior > novo:
                    raise _KmParaTras(maior)
                veiculo.refresh_from_db(fields=["km_atual"])
                if veiculo.km_atual == antigo:
                    veiculo.km_atual = maior
                    veiculo.save(update_fields=["km_atual"])
    except _KmParaTras as erro:
        setattr(objeto, campo, antigo)
        return erro.args[0]
    return None


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
