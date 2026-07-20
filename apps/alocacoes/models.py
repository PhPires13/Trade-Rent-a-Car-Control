from django.core.exceptions import ValidationError
from django.db import models, transaction
from simple_history.models import HistoricalRecords

from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente


class Alocacao(models.Model):
    """Vínculo veículo↔cliente com valor semanal (docs.md §4.2).

    A caução aqui é só o valor acordado; o extrato/movimentações entram na etapa 4.
    """

    class Status(models.TextChoices):
        ATIVA = "ativa", "Ativa"
        ENCERRADA = "encerrada", "Encerrada"

    class LimiteKm(models.TextChoices):
        ILIMITADO = "ilimitado", "Ilimitado"
        LIMITADO = "limitado", "Limitado"

    veiculo = models.ForeignKey(
        Veiculo, verbose_name="veículo", on_delete=models.PROTECT, related_name="alocacoes"
    )
    cliente = models.ForeignKey(
        Cliente, verbose_name="cliente", on_delete=models.PROTECT, related_name="alocacoes"
    )
    data_inicio = models.DateField("data de início")
    data_termino = models.DateField("data de término", null=True, blank=True)
    valor_semanal = models.DecimalField("valor semanal (R$)", max_digits=8, decimal_places=2)
    dia_vencimento = models.IntegerField(
        "dia de vencimento semanal",
        choices=Cliente.DiaSemana.choices,
        help_text="Padrão: dia da semana do início da locação",
    )
    caucao_valor = models.DecimalField(
        "caução acordada (R$)", max_digits=8, decimal_places=2, null=True, blank=True
    )
    km_entrega = models.PositiveIntegerField("KM na entrega")
    km_devolucao = models.PositiveIntegerField("KM na devolução", null=True, blank=True)
    limite_km = models.CharField(
        "limite de km", max_length=10, choices=LimiteKm.choices, default=LimiteKm.ILIMITADO
    )
    franquia_km_mensal = models.PositiveIntegerField(
        "franquia mensal (km)", null=True, blank=True, help_text="Somente quando limitado"
    )
    taxa_km_excedido = models.DecimalField(
        "taxa por km excedido (R$)", max_digits=6, decimal_places=2, null=True, blank=True
    )
    status = models.CharField("status", max_length=10, choices=Status.choices, default=Status.ATIVA)
    observacoes = models.TextField("observações", blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "alocação"
        verbose_name_plural = "alocações"
        ordering = ["-data_inicio"]
        constraints = [
            models.UniqueConstraint(
                fields=["veiculo"],
                condition=models.Q(status="ativa"),
                name="uma_alocacao_ativa_por_veiculo",
            )
        ]

    def __str__(self):
        return f"{self.veiculo.placa} → {self.cliente.nome} ({self.get_status_display()})"

    def save(self, *args, **kwargs):
        if self.dia_vencimento is None and self.data_inicio:
            self.dia_vencimento = self.data_inicio.weekday()
        criando = self._state.adding
        with transaction.atomic():
            super().save(*args, **kwargs)
            if criando and self.status == self.Status.ATIVA:
                self.veiculo.status = Veiculo.Status.ALOCADO
                if self.km_entrega > self.veiculo.km_atual:
                    self.veiculo.km_atual = self.km_entrega
                self.veiculo.save(update_fields=["status", "km_atual"])

    def clean(self):
        erros = {}
        if self._state.adding:
            if self.veiculo_id and self.veiculo.status != Veiculo.Status.DISPONIVEL:
                erros["veiculo"] = (
                    f"Veículo {self.veiculo.placa} não está disponível "
                    f"(status: {self.veiculo.get_status_display()})."
                )
            if self.veiculo_id and self.veiculo.uso != Veiculo.Uso.LOCACAO:
                erros["veiculo"] = "Veículo fora de locação não pode ser alocado."
            if self.cliente_id and self.cliente.status == Cliente.Status.INATIVO:
                erros["cliente"] = "Cliente inativo não pode receber veículo."
        if self.limite_km == self.LimiteKm.LIMITADO and not self.franquia_km_mensal:
            erros["franquia_km_mensal"] = "Informe a franquia mensal para limite de km."
        if erros:
            raise ValidationError(erros)

    @transaction.atomic
    def encerrar(self, data_termino, km_devolucao):
        """Encerra a alocação e libera o veículo (docs.md §4.2).

        O acerto de caução entra na etapa 4.
        """
        if self.status != self.Status.ATIVA:
            raise ValidationError("Alocação já encerrada.")
        if self.trocas.filter(data_devolucao__isnull=True).exists():
            raise ValidationError("Devolva o carro substituto antes de encerrar a alocação.")
        if km_devolucao < self.km_entrega:
            raise ValidationError("KM de devolução menor que o KM de entrega.")
        self.data_termino = data_termino
        self.km_devolucao = km_devolucao
        self.status = self.Status.ENCERRADA
        self.save()
        veiculo = self.veiculo
        veiculo.status = Veiculo.Status.DISPONIVEL
        if km_devolucao > veiculo.km_atual:
            veiculo.km_atual = km_devolucao
        veiculo.save(update_fields=["status", "km_atual"])

    @property
    def troca_ativa(self):
        return self.trocas.filter(data_devolucao__isnull=True).first()


class TrocaTemporaria(models.Model):
    """Carro substituto emprestado durante conserto, sem encerrar a alocação (docs.md §4.2)."""

    alocacao = models.ForeignKey(
        Alocacao, verbose_name="alocação", on_delete=models.PROTECT, related_name="trocas"
    )
    veiculo_substituto = models.ForeignKey(
        Veiculo,
        verbose_name="veículo substituto",
        on_delete=models.PROTECT,
        related_name="trocas_como_substituto",
    )
    data_retirada = models.DateField("data de retirada")
    data_devolucao = models.DateField("data de devolução", null=True, blank=True)
    km_retirada = models.PositiveIntegerField("KM na retirada")
    km_devolucao = models.PositiveIntegerField("KM na devolução", null=True, blank=True)
    motivo = models.CharField(
        "motivo", max_length=200, blank=True, help_text="Ex.: manutenção do carro principal"
    )
    valor_semanal_ajustado = models.DecimalField(
        "valor semanal durante a troca (R$)",
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Vazio = mantém o valor da alocação; muda quando a categoria é diferente",
    )
    observacoes = models.TextField("observações", blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "troca temporária"
        verbose_name_plural = "trocas temporárias"
        ordering = ["-data_retirada"]

    def __str__(self):
        return (
            f"{self.alocacao.cliente.nome} com {self.veiculo_substituto.placa} "
            f"desde {self.data_retirada:%d/%m/%Y}"
        )

    def save(self, *args, **kwargs):
        criando = self._state.adding
        with transaction.atomic():
            super().save(*args, **kwargs)
            if criando:
                substituto = self.veiculo_substituto
                substituto.status = Veiculo.Status.ALOCADO
                if self.km_retirada > substituto.km_atual:
                    substituto.km_atual = self.km_retirada
                substituto.save(update_fields=["status", "km_atual"])

    def clean(self):
        erros = {}
        if self._state.adding:
            if self.alocacao_id and self.alocacao.status != Alocacao.Status.ATIVA:
                erros["alocacao"] = "A alocação precisa estar ativa."
            if (
                self.alocacao_id
                and self.alocacao.trocas.filter(data_devolucao__isnull=True).exists()
            ):
                erros["alocacao"] = "Já existe uma troca em andamento nesta alocação."
            if (
                self.veiculo_substituto_id
                and self.veiculo_substituto.status != Veiculo.Status.DISPONIVEL
            ):
                erros["veiculo_substituto"] = (
                    f"Substituto {self.veiculo_substituto.placa} não está disponível."
                )
            if (
                self.veiculo_substituto_id
                and self.alocacao_id
                and self.veiculo_substituto_id == self.alocacao.veiculo_id
            ):
                erros["veiculo_substituto"] = "O substituto não pode ser o próprio carro."
        if erros:
            raise ValidationError(erros)

    @transaction.atomic
    def devolver(self, data_devolucao, km_devolucao):
        if self.data_devolucao:
            raise ValidationError("Substituto já devolvido.")
        if km_devolucao < self.km_retirada:
            raise ValidationError("KM de devolução menor que o KM de retirada.")
        self.data_devolucao = data_devolucao
        self.km_devolucao = km_devolucao
        self.save()
        substituto = self.veiculo_substituto
        substituto.status = Veiculo.Status.DISPONIVEL
        if km_devolucao > substituto.km_atual:
            substituto.km_atual = km_devolucao
        substituto.save(update_fields=["status", "km_atual"])

    @property
    def ativa(self):
        return self.data_devolucao is None
