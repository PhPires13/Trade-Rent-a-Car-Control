from datetime import date
from uuid import uuid4

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.core.validators import MinValueValidator
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme, urlencode

from apps.alocacoes.services import cliente_vigente
from apps.alocacoes.views import _avisar_acerto_das_semanas
from apps.campos import CampoDinheiro, FormularioBR
from apps.financeiro.views import _envio_unico
from apps.formatos import brl
from apps.frota.models import Veiculo, normalizar_placa

from . import services
from .models import AuxilioMotorista, Sinistro

# input type="date" só entende AAAA-MM-DD; no formato pt-BR (01/07/2026) a data
# some da tela ao editar um sinistro já salvo.
_DATA = {"type": "date"}

#: franquia e auxílio são dinheiro pago/recebido: negativo viraria crédito falso
SEM_NEGATIVO = MinValueValidator(0, message="O valor não pode ser negativo.")


class SinistroForm(FormularioBR, forms.ModelForm):
    """Novo sinistro e edição, em blocos com texto comum (proposta #10)."""

    # Blocos da tela, na ordem do registro. Na edição a situação vai para o topo.
    BLOCOS = [
        {
            "chave": "ocorrido",
            "titulo": "O que aconteceu",
            "dica": "",
            "campos": [
                "veiculo",
                "data",
                "cliente",
                "tipo",
                "envolvido",
                "descricao",
                "boletim_ocorrencia",
            ],
        },
        {
            "chave": "protecao",
            "titulo": "Acionou a proteção?",
            "dica": "Marque se o ocorrido foi comunicado à Auto Truck (abriu um evento).",
            "campos": ["acionou_protecao", "data_evento", "franquia_valor"],
        },
        {
            "chave": "custo",
            "titulo": "Quem paga",
            "dica": "",
            "campos": ["responsabilidade", "responsavel_custo"],
        },
        {
            "chave": "situacao",
            "titulo": "Situação",
            "dica": "",
            "campos": ["status", "observacoes"],
        },
    ]
    CAMPOS_LARGOS = {"descricao", "observacoes", "acionou_protecao", "status"}
    CAMPOS_DA_PROTECAO = {"data_evento", "franquia_valor"}  # só aparecem com a caixa marcada

    class Meta:
        model = Sinistro
        fields = [
            "veiculo",
            "data",
            "cliente",
            "tipo",
            "envolvido",
            "responsabilidade",
            "descricao",
            "boletim_ocorrencia",
            "acionou_protecao",
            "data_evento",
            "franquia_valor",
            "responsavel_custo",
            "status",
            "observacoes",
        ]
        # Rótulos em texto comum, com o termo técnico entre parênteses. Ficam no
        # formulário (e não no modelo) para não gerar migração.
        labels = {
            "veiculo": "Carro",
            "data": "Data do ocorrido",
            "cliente": "Motorista (cliente)",
            "tipo": "O que foi",
            "envolvido": "Quem está envolvido",
            "responsabilidade": "De quem foi a culpa",
            "descricao": "Como aconteceu",
            "boletim_ocorrencia": "Número do boletim de ocorrência (BO)",
            "acionou_protecao": "Comunicamos à proteção veicular (abriu evento na Auto Truck)",
            "data_evento": "Data em que o evento foi aberto",
            "franquia_valor": "Valor da franquia cobrada (cota), em R$",
            "responsavel_custo": "Quem paga o conserto",
            "status": "Situação do sinistro",
            "observacoes": "Observações",
        }
        widgets = {
            "data": forms.DateInput(format="%Y-%m-%d", attrs=_DATA),
            "data_evento": forms.DateInput(format="%Y-%m-%d", attrs=_DATA),
            "descricao": forms.Textarea(attrs={"rows": 3}),
            "observacoes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        campos = self.fields
        campos["cliente"].required = False
        campos["cliente"].empty_label = "Preencher sozinho pela data"
        campos[
            "cliente"
        ].help_text = (
            "Deixe em “Preencher sozinho” — o sistema usa quem estava com o carro na data."
        )
        campos["veiculo"].queryset = Veiculo.objects.order_by("placa")
        campos["envolvido"].choices = [
            ("", "---------"),
            (Sinistro.Envolvido.ASSOCIADO, "Só o nosso carro (associado)"),
            (Sinistro.Envolvido.TERCEIRO, "Outro carro ou pessoa (terceiro)"),
        ]
        campos["responsabilidade"].choices = [
            (Sinistro.Responsabilidade.CLIENTE, "Do nosso motorista (cliente)"),
            (Sinistro.Responsabilidade.TERCEIRO, "De outra pessoa (terceiro)"),
            (Sinistro.Responsabilidade.INDEFINIDA, "Ainda não se sabe"),
        ]
        campos["responsavel_custo"].choices = [
            ("empresa", "Empresa"),
            ("cliente", "Cliente (motorista)"),
        ]
        campos["boletim_ocorrencia"].help_text = "Se ainda não tem, preencha depois em Editar."
        campos[
            "franquia_valor"
        ].help_text = "Zero quando entrou nas franquias grátis do plano. Pode preencher depois."
        campos["franquia_valor"].validators.append(SEM_NEGATIVO)
        # a caixa liga/desliga a data do evento e a franquia na tela (Alpine)
        campos["acionou_protecao"].widget.attrs["x-model"] = "protecao"

    def clean_data(self):
        dia = self.cleaned_data["data"]
        if dia and dia > date.today():
            raise ValidationError("A data do ocorrido não pode ser no futuro.")
        return dia

    def clean(self):
        dados = super().clean()
        if not dados.get("acionou_protecao"):
            # campos escondidos na tela não podem ficar com valor velho gravado —
            # nem barrar o salvar com um erro que a pessoa nem enxerga
            for nome in self.CAMPOS_DA_PROTECAO:
                self._errors.pop(nome, None)
                dados[nome] = None
        evento, ocorrido = dados.get("data_evento"), dados.get("data")
        if evento and ocorrido and evento < ocorrido:
            # o evento na Auto Truck é aberto por causa do ocorrido: antes dele é erro
            # de digitação (ano ou mês trocado)
            self.add_error(
                "data_evento",
                f"O evento não pode ter sido aberto antes do ocorrido ({ocorrido:%d/%m/%Y}).",
            )
        return dados

    def save(self, commit=True):
        sinistro = super().save(commit=False)
        # Na edição, trocar o carro ou a data sem mexer no motorista recalcula quem
        # estava com o carro — senão ficaria o motorista do dia antigo.
        if (
            self.instance.pk
            and {"veiculo", "data"} & set(self.changed_data)
            and "cliente" not in self.changed_data
        ):
            sinistro.cliente = None  # o save() do modelo preenche pelo cliente_vigente
        if commit:
            sinistro.save()
        return sinistro

    def blocos(self):
        """Blocos com os campos já prontos para o template; na edição, situação no topo."""
        ordem = list(self.BLOCOS)
        if self.instance.pk:
            ordem.sort(key=lambda bloco: bloco["chave"] != "situacao")
        return [{**bloco, "campos": [self[nome] for nome in bloco["campos"]]} for bloco in ordem]


def _valor_digitado(request):
    """Valor opcional do POST ("1.518,00"), validado como o campo do auxílio.

    Só o dinheiro_br aceitava "99999999" ou "1e10": o número era gravado e a
    lista de sinistros passava a dar erro ao lê-lo de volta. Vazio → None.
    """
    campo = AuxilioMotorista._meta.get_field("valor").formfield(form_class=CampoDinheiro)
    campo.validators.append(SEM_NEGATIVO)
    return campo.clean(request.POST.get("valor", "").strip())


def _volta(request):
    """Para onde voltar depois de salvar: a lista com os filtros que estavam abertos.

    Só aceita endereço deste site (evita redirecionar para fora pelo ?volta=).
    """
    destino = request.POST.get("volta") or request.GET.get("volta") or ""
    if destino and url_has_allowed_host_and_scheme(
        destino, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return destino
    return reverse("sinistros:lista")


def lista(request):
    sinistros = Sinistro.objects.select_related("veiculo", "cliente").prefetch_related(
        "manutencoes", "auxilios"
    )
    status = request.GET.get("status")
    if status:
        sinistros = sinistros.filter(status=status)
    placa = request.GET.get("veiculo", "").strip()
    if placa:
        # o banco grava a placa sem hífen/espaço: "ABC-1D23" precisa achar "ABC1D23"
        sinistros = sinistros.filter(veiculo__placa__icontains=normalizar_placa(placa))
    pagina = Paginator(sinistros, 25).get_page(request.GET.get("pagina"))
    return render(
        request,
        "sinistros/lista.html",
        {
            "sinistros": pagina.object_list,
            "pagina": pagina,
            "filtros": urlencode({"status": status or "", "veiculo": placa}),
            "filtro_veiculo": placa,
            "statuses": Sinistro.Status.choices,
            "filtro": status or "",
            "aqui": request.get_full_path(),
        },
    )


def _mensagem_motorista(sinistro):
    return sinistro.cliente.nome if sinistro.cliente else "nenhum cliente na data"


def _aplicar_roubo(request, sinistro):
    """Roubo: tira o carro do motorista na data e deixa-o Inativo (com aviso na tela)."""
    try:
        resultado = services.registrar_roubo(sinistro)
    except ValidationError as erro:
        messages.warning(
            request,
            "O sinistro foi salvo, mas a locação não foi encerrada sozinha: "
            + "; ".join(erro.messages)
            + ". Depois de ajustar, use o botão “Aplicar o roubo” na edição do sinistro.",
        )
        return
    placa = sinistro.veiculo.placa
    sinistro.veiculo.refresh_from_db(fields=["status"])
    if sinistro.veiculo.status == Veiculo.Status.INATIVO:
        texto = f"Roubo registrado: {placa} ficou Inativo até ser recuperado."
    else:
        texto = f"Roubo registrado. {placa} continua como {sinistro.veiculo.get_status_display()}."
    alocacao = resultado.alocacao
    if alocacao is not None:
        texto += (
            f" A locação de {alocacao.cliente.nome} foi encerrada em "
            f"{alocacao.data_termino:%d/%m/%Y} — o aluguel para de ser cobrado."
        )
    if resultado.troca is not None:
        troca = resultado.troca
        texto += (
            f" {placa} era o carro substituto de {troca.alocacao.cliente.nome}: a troca foi "
            f"encerrada em {troca.data_devolucao:%d/%m/%Y}. A locação dele continua com o "
            f"{troca.alocacao.veiculo.placa} — se ele ficou sem carro, combine com ele e "
            "ajuste em Alocações."
        )
    if resultado.locacao_continua is not None:
        continua = resultado.locacao_continua
        texto += (
            f" A locação de {continua.cliente.nome} continua, porque ele está com o carro "
            f"substituto {continua.troca_ativa.veiculo_substituto.placa}."
        )
    messages.warning(request, texto)
    if alocacao is not None:
        # o que já tinha sido pago por semanas depois do roubo (crédito/caução)
        _avisar_acerto_das_semanas(request, alocacao)


def _deixou_de_ser_roubo(request, sinistro, veiculo_id=None):
    """Tipo (ou carro) corrigido no roubo: o carro de antes sai de Inativo."""
    if services.deixou_de_ser_roubo(sinistro, veiculo_id):
        veiculo = Veiculo.objects.get(pk=veiculo_id or sinistro.veiculo_id)
        motivo = "deixou de ser roubo" if veiculo_id is None else "o roubo é de outro carro"
        messages.info(
            request,
            f"Como {motivo}, {veiculo.placa} voltou a ficar "
            f"{veiculo.get_status_display()}. "
            "Se o roubo tinha encerrado uma locação, ela continua encerrada — ajuste em "
            "Alocações se precisar.",
        )


#: chave da sessão com os códigos de formulário de "Novo sinistro" já gravados
SESSAO_TOKENS_SINISTRO = "sinistro_tokens_usados"


def novo(request):
    # código único por formulário (como na ND): reenviar não grava em dobro
    token = request.POST.get("token", "") if request.method == "POST" else ""
    form = SinistroForm(request.POST or None, initial={"data": date.today()})
    if request.method == "POST" and form.is_valid():
        # linha da sessão travada até gravar: o 2º envio de um clique duplo espera
        with _envio_unico(request, SESSAO_TOKENS_SINISTRO, token) as repetido:
            if repetido:
                messages.warning(
                    request,
                    "Este sinistro já tinha sido registrado — o envio repetido foi ignorado.",
                )
                return redirect("sinistros:lista")
            sinistro = form.save()
        messages.success(
            request, f"Sinistro registrado — motorista: {_mensagem_motorista(sinistro)}."
        )
        if sinistro.tipo == Sinistro.Tipo.ROUBO:
            _aplicar_roubo(request, sinistro)
        return redirect("sinistros:lista")
    return render(
        request,
        "sinistros/novo.html",
        {"form": form, "volta": _volta(request), "token": token or uuid4().hex},
    )


def editar(request, sinistro_id):
    sinistro = get_object_or_404(
        Sinistro.objects.select_related(
            "veiculo", "cliente", "cobranca_franquia__cliente"
        ).prefetch_related("manutencoes", "auxilios"),
        pk=sinistro_id,
    )
    # o is_valid() já troca o tipo na instância: guarda o de antes
    era_roubo_aberto = sinistro.tipo == Sinistro.Tipo.ROUBO and not sinistro.data_recuperacao
    veiculo_antes = sinistro.veiculo_id
    form = SinistroForm(request.POST or None, instance=sinistro)
    if request.method == "POST" and form.is_valid():
        virou_roubo = (
            "tipo" in form.changed_data and form.cleaned_data["tipo"] == Sinistro.Tipo.ROUBO
        )
        sinistro = form.save()
        trocou_carro = sinistro.veiculo_id != veiculo_antes
        if era_roubo_aberto and (sinistro.tipo != Sinistro.Tipo.ROUBO or trocou_carro):
            # o carro de antes não foi roubado (tipo ou carro corrigido): sai de Inativo
            _deixou_de_ser_roubo(request, sinistro, veiculo_antes if trocou_carro else None)
        if era_roubo_aberto and sinistro.tipo == Sinistro.Tipo.ROUBO and trocou_carro:
            virou_roubo = True  # o roubo passa para o carro certo
        messages.success(
            request,
            f"Sinistro de {sinistro.veiculo.placa} salvo — situação: "
            f"{sinistro.get_status_display().lower()}, motorista: {_mensagem_motorista(sinistro)}.",
        )
        if virou_roubo:
            _aplicar_roubo(request, sinistro)
        return redirect(_volta(request))
    return render(
        request,
        "sinistros/editar.html",
        {"form": form, "sinistro": sinistro, "volta": _volta(request)},
    )


def quem_estava(request):
    """Pedaço de tela (htmx): com quem o carro estava na data escolhida."""
    pk = request.GET.get("veiculo", "")
    # ?veiculo=abc (ou lixo vindo do navegador) dava erro 500 no filtro por pk
    veiculo = Veiculo.objects.filter(pk=pk).first() if pk.isdigit() else None
    try:
        dia = date.fromisoformat(request.GET.get("data", ""))
    except ValueError:
        dia = None
    cliente = cliente_vigente(veiculo, dia) if veiculo and dia else None
    return render(
        request,
        "sinistros/_quem_estava.html",
        {"veiculo": veiculo, "dia": dia, "cliente": cliente},
    )


def solicitar_auxilio(request, sinistro_id):
    sinistro = get_object_or_404(Sinistro, pk=sinistro_id)
    if request.method == "POST":
        with transaction.atomic():
            # trava o sinistro: dois envios ao mesmo tempo não criam dois auxílios
            sinistro = Sinistro.objects.select_for_update().get(pk=sinistro.pk)
            return _solicitar_auxilio(request, sinistro)
    return redirect(_volta(request))


def _solicitar_auxilio(request, sinistro):
    # clique duplo / F5 no POST criava dois pedidos para o mesmo sinistro
    if sinistro.auxilios.exists():
        messages.info(request, "O auxílio deste sinistro já estava registrado.")
        return redirect(_volta(request))
    if sinistro.tipo != Sinistro.Tipo.COLISAO:
        messages.error(request, "O auxílio motorista só vale para colisão/avaria.")
        return redirect(_volta(request))
    if not sinistro.acionou_protecao:
        messages.error(
            request,
            "O auxílio motorista só é pago com a proteção acionada (evento aberto na "
            "Auto Truck). Marque isso no sinistro antes de pedir.",
        )
        return redirect(_volta(request))
    try:
        valor = _valor_digitado(request)
    except ValidationError as erro:
        messages.error(request, "; ".join(erro.messages))
        return redirect(_volta(request))
    AuxilioMotorista.objects.create(
        sinistro=sinistro,
        valor=valor,
        status=AuxilioMotorista.Status.SOLICITADO,
        data_solicitacao=date.today(),
    )
    messages.success(request, "Auxílio motorista registrado como solicitado.")
    return redirect(_volta(request))


def receber_auxilio(request, auxilio_id):
    auxilio = get_object_or_404(AuxilioMotorista, pk=auxilio_id)
    if request.method == "POST":
        if auxilio.status == AuxilioMotorista.Status.RECEBIDO:
            # repetir o POST não pode mudar a data nem o valor já recebidos
            messages.info(request, "Este auxílio já estava marcado como recebido.")
            return redirect(_volta(request))
        try:
            valor = _valor_digitado(request)
        except ValidationError as erro:
            messages.error(request, "; ".join(erro.messages))
            return redirect(_volta(request))
        if valor is None and auxilio.valor is None:
            messages.error(request, "Informe o valor recebido (ex.: 1.518,00).")
            return redirect(_volta(request))
        if valor is not None:  # vazio mantém o valor informado na solicitação
            auxilio.valor = valor
        auxilio.status = AuxilioMotorista.Status.RECEBIDO
        auxilio.data_recebimento = date.today()
        auxilio.save()
        messages.success(
            request,
            f"Auxílio de {brl(auxilio.valor)} recebido — outro crédito, fora da base do DAS.",
        )
    return redirect(_volta(request))


def cobrar_franquia(request, sinistro_id):
    """Botão "Cobrar do cliente": a franquia vira cobrança para o motorista."""
    sinistro = get_object_or_404(Sinistro, pk=sinistro_id)
    if request.method == "POST":
        try:
            cobranca = services.cobrar_franquia(sinistro)
        except ValidationError as erro:
            messages.error(request, "; ".join(erro.messages))
        else:
            messages.success(
                request,
                f"Franquia de {brl(cobranca.valor)} cobrada de {cobranca.cliente.nome} — "
                f"vence em {cobranca.vencimento:%d/%m/%Y} (próximo dia de pagamento).",
            )
    return redirect(_volta(request))


def carro_recuperado(request, sinistro_id):
    """Botão "Carro recuperado" do roubo: o carro volta a ficar Disponível."""
    sinistro = get_object_or_404(Sinistro.objects.select_related("veiculo"), pk=sinistro_id)
    if request.method == "POST":
        try:
            services.carro_recuperado(sinistro)
        except ValidationError as erro:
            messages.info(request, "; ".join(erro.messages))
        else:
            sinistro.veiculo.refresh_from_db(fields=["status"])
            if sinistro.veiculo.status == Veiculo.Status.DISPONIVEL:
                texto = f"{sinistro.veiculo.placa} recuperado — já está Disponível para alugar."
            else:
                texto = (
                    f"{sinistro.veiculo.placa} recuperado em {date.today():%d/%m/%Y}. A situação "
                    f"do carro agora é {sinistro.veiculo.get_status_display()}."
                )
            messages.success(request, texto)
    return redirect(_volta(request))


def aplicar_roubo(request, sinistro_id):
    """Botão "Aplicar o roubo": refaz o encerramento que não deu certo ao salvar."""
    sinistro = get_object_or_404(Sinistro.objects.select_related("veiculo"), pk=sinistro_id)
    if request.method == "POST":
        if sinistro.tipo != Sinistro.Tipo.ROUBO or sinistro.data_recuperacao:
            messages.info(request, "Este sinistro não é um roubo em aberto.")
        else:
            _aplicar_roubo(request, sinistro)
    return redirect(_volta(request))
