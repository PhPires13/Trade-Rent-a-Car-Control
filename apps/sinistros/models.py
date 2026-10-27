from datetime import timedelta

from django.db import models
from django.utils import timezone
from simple_history.models import HistoricalRecords

from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente

DIAS_CARENCIA_AUXILIO = 7  # decisão nº 10: auxílio quando conserto de colisão passa de 7 dias


class Sinistro(models.Model):
    """Colisão, dano ou roubo, com evento na Auto Truck (docs.md §4.6)."""

    class Tipo(models.TextChoices):
        COLISAO = "colisao", "Colisão/avaria"
        ROUBO = "roubo", "Roubo/furto"
        OUTRO = "outro", "Outro"

    class Envolvido(models.TextChoices):
        ASSOCIADO = "associado", "Associado (nosso condutor)"
        TERCEIRO = "terceiro", "Terceiro"

    class Responsabilidade(models.TextChoices):
        CLIENTE = "cliente", "Cliente/associado"
        TERCEIRO = "terceiro", "Terceiro"
        INDEFINIDA = "indefinida", "Indefinida"

    class Status(models.TextChoices):
        ABERTO = "aberto", "Aberto"
        REGULARIZACAO = "regularizacao", "Em regularização"
        CONCLUIDO = "concluido", "Concluído"

    veiculo = models.ForeignKey(
        Veiculo, verbose_name="veículo", on_delete=models.PROTECT, related_name="sinistros"
    )
    data = models.DateField("data")
    cliente = models.ForeignKey(
        Cliente,
        verbose_name="motorista (cliente)",
        on_delete=models.PROTECT,
        related_name="sinistros",
        null=True,
        blank=True,
        help_text="Preenchido automaticamente por quem estava com o carro na data",
    )
    tipo = models.CharField("tipo", max_length=10, choices=Tipo.choices, default=Tipo.COLISAO)
    envolvido = models.CharField("envolvido", max_length=10, choices=Envolvido.choices)
    responsabilidade = models.CharField(
        "responsabilidade",
        max_length=10,
        choices=Responsabilidade.choices,
        default=Responsabilidade.INDEFINIDA,
    )
    descricao = models.TextField("descrição do ocorrido", blank=True)
    boletim_ocorrencia = models.CharField("boletim de ocorrência (nº)", max_length=40, blank=True)

    acionou_protecao = models.BooleanField("acionou a proteção (evento Auto Truck)?", default=False)
    data_evento = models.DateField("data do evento na associação", null=True, blank=True)
    franquia_valor = models.DecimalField(
        "franquia/cota do evento (R$)",
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Zero quando coberta pelas franquias gratuitas do plano",
    )

    responsavel_custo = models.CharField(
        "responsável pelo custo",
        max_length=10,
        choices=[("empresa", "Empresa"), ("cliente", "Cliente")],
        default="empresa",
    )
    status = models.CharField(
        "status", max_length=15, choices=Status.choices, default=Status.ABERTO
    )
    observacoes = models.TextField("observações", blank=True)
    cobranca_franquia = models.OneToOneField(
        "financeiro.Cobranca",
        verbose_name="cobrança da franquia ao cliente",
        on_delete=models.PROTECT,
        related_name="sinistro_franquia",
        null=True,
        blank=True,
    )
    data_recuperacao = models.DateField(
        "data em que o carro foi recuperado (roubo)", null=True, blank=True
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = "sinistro"
        ordering = ["-data"]

    def __str__(self):
        return f"{self.veiculo.placa} — {self.get_tipo_display()} em {self.data:%d/%m/%Y}"

    def save(self, *args, **kwargs):
        if self.cliente_id is None and self.veiculo_id and self.data:
            from apps.alocacoes.services import cliente_vigente

            self.cliente = cliente_vigente(self.veiculo, self.data)
        super().save(*args, **kwargs)

    @property
    def boletim_numero(self):
        """Nº do BO sem um "BO" digitado no começo — a tela já mostra "BO" antes.

        Os registros antigos foram gravados antes da limpeza no formulário e
        apareciam como "BO BO 2026-123456".
        """
        from .views import _PREFIXO_BO

        return _PREFIXO_BO.sub("", self.boletim_ocorrencia or "").strip()

    @property
    def dias_parado(self):
        """Dias na oficina somando todas as manutenções do sinistro (docs.md §4.6).

        Funilaria e depois mecânica contam juntas — antes valia só a passagem
        mais longa e o auxílio podia ser perdido. Cada passagem conta como a da
        manutenção (saída − entrada; em aberto, até hoje) e um dia que caia em
        duas passagens ao mesmo tempo só conta uma vez.
        """
        hoje = timezone.localdate()
        dias = set()
        for manutencao in self.manutencoes.all():
            if manutencao.data_entrada:
                fim = manutencao.data_saida or hoje
                total = (fim - manutencao.data_entrada).days
                dias.update(manutencao.data_entrada + timedelta(days=n) for n in range(total))
        return len(dias)

    @property
    def auxilio_disponivel(self):
        """Colisão com a proteção acionada, parada além da carência e sem auxílio.

        A associação só paga o auxílio quando há evento aberto: sem a proteção
        acionada o aviso só fazia o dono pedir à toa.
        """
        return (
            self.tipo == self.Tipo.COLISAO
            and self.acionou_protecao
            and self.dias_parado > DIAS_CARENCIA_AUXILIO
            and not self.auxilios.exists()
        )

    @property
    def franquia_a_cobrar(self):
        """Franquia que o motorista deve pagar e que ainda não virou cobrança."""
        return (
            self.responsavel_custo == "cliente"
            and bool(self.franquia_valor)
            and (
                self.cobranca_franquia_id is None
                or self.cobranca_franquia.status == "cancelada"  # cancelada: cobrar de novo
            )
        )


class AuxilioMotorista(models.Model):
    """Auxílio motorista profissional — um salário mínimo quando o conserto de
    colisão passa de 7 dias (decisão nº 10). Crédito fora da base do DAS."""

    class Status(models.TextChoices):
        A_SOLICITAR = "a_solicitar", "A solicitar"
        SOLICITADO = "solicitado", "Solicitado"
        RECEBIDO = "recebido", "Recebido"

    sinistro = models.ForeignKey(
        Sinistro, verbose_name="sinistro", on_delete=models.PROTECT, related_name="auxilios"
    )
    valor = models.DecimalField(
        "valor (R$)",
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Um salário mínimo cheio",
    )
    status = models.CharField(
        "status", max_length=12, choices=Status.choices, default=Status.A_SOLICITAR
    )
    data_solicitacao = models.DateField("data da solicitação", null=True, blank=True)
    data_recebimento = models.DateField("data do recebimento", null=True, blank=True)
    observacoes = models.TextField("observações", blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "auxílio motorista"
        verbose_name_plural = "auxílios motorista"
        ordering = ["-pk"]

    def __str__(self):
        return f"Auxílio — {self.sinistro} ({self.get_status_display()})"
