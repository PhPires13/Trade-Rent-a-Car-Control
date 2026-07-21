from django.db import models
from simple_history.models import HistoricalRecords

from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente

# Prazo de paralisação para o auxílio motorista profissional (docs.md decisão nº 10).
DIAS_PARA_AUXILIO = 7


class Sinistro(models.Model):
    """Acidente, dano ou roubo/furto (docs.md §4.6)."""

    class Tipo(models.TextChoices):
        COLISAO = "colisao", "Colisão/avaria"
        ROUBO_FURTO = "roubo_furto", "Roubo/furto"
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
    motorista = models.ForeignKey(
        Cliente,
        verbose_name="motorista",
        on_delete=models.PROTECT,
        related_name="sinistros",
        null=True,
        blank=True,
        help_text="Cliente vigente na data (preenchido automaticamente)",
    )
    tipo = models.CharField("tipo", max_length=15, choices=Tipo.choices, default=Tipo.COLISAO)
    envolvido = models.CharField("envolvido", max_length=10, choices=Envolvido.choices)
    responsabilidade = models.CharField(
        "responsabilidade",
        max_length=12,
        choices=Responsabilidade.choices,
        default=Responsabilidade.INDEFINIDA,
    )
    descricao = models.TextField("descrição do ocorrido", blank=True)
    boletim_ocorrencia = models.CharField("boletim de ocorrência (nº)", max_length=40, blank=True)

    acionou_protecao = models.BooleanField("acionou a proteção (evento Auto Truck)?", default=False)
    data_evento = models.DateField("data do evento", null=True, blank=True)
    franquia_valor = models.DecimalField(
        "cota/franquia usada (R$)",
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Pode ser 0 se coberta pela taxa de franquias gratuitas",
    )

    situacao_veiculo = models.CharField(
        "situação do veículo (roubo/furto)", max_length=120, blank=True
    )
    status = models.CharField(
        "status", max_length=15, choices=Status.choices, default=Status.ABERTO
    )
    observacoes = models.TextField("observações", blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "sinistro"
        ordering = ["-data"]

    def __str__(self):
        return f"{self.veiculo.placa} — {self.get_tipo_display()} em {self.data:%d/%m/%Y}"


class AuxilioMotorista(models.Model):
    """Auxílio motorista profissional da associação (docs.md §4.6, decisão nº 10).

    Colisão com o veículo parado > 7 dias → a associação paga um salário mínimo.
    Crédito fora da base do DAS.
    """

    class Status(models.TextChoices):
        A_SOLICITAR = "a_solicitar", "A solicitar"
        SOLICITADO = "solicitado", "Solicitado"
        RECEBIDO = "recebido", "Recebido"

    sinistro = models.ForeignKey(
        Sinistro, verbose_name="sinistro", on_delete=models.PROTECT, related_name="auxilios"
    )
    dias_parado = models.PositiveIntegerField("dias parado", null=True, blank=True)
    valor = models.DecimalField(
        "valor (R$)",
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Um salário mínimo (docs.md decisão nº 10)",
    )
    status = models.CharField(
        "status", max_length=12, choices=Status.choices, default=Status.A_SOLICITAR
    )
    data_recebimento = models.DateField("data do recebimento", null=True, blank=True)
    observacoes = models.TextField("observações", blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "auxílio motorista profissional"
        verbose_name_plural = "auxílios motorista profissional"
        ordering = ["-id"]

    def __str__(self):
        return f"Auxílio — {self.sinistro.veiculo.placa} ({self.get_status_display()})"
