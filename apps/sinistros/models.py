from datetime import date

from django.db import models
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
    def dias_parado(self):
        """Maior paralisação entre as manutenções vinculadas (docs.md §4.6)."""
        dias = 0
        for manutencao in self.manutencoes.all():
            if manutencao.data_entrada:
                fim = manutencao.data_saida or date.today()
                dias = max(dias, (fim - manutencao.data_entrada).days)
        return dias

    @property
    def auxilio_disponivel(self):
        """Colisão parada além da carência e sem auxílio registrado."""
        return (
            self.tipo == self.Tipo.COLISAO
            and self.dias_parado > DIAS_CARENCIA_AUXILIO
            and not self.auxilios.exists()
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
