from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Sum
from simple_history.models import HistoricalRecords

from apps.alocacoes.models import Alocacao
from apps.pessoas.models import Cliente

CENTAVO = Decimal("0.01")


class Cobranca(models.Model):
    """Valor que o cliente deve (docs.md §4.3). Toda origem vira uma cobrança."""

    class Origem(models.TextChoices):
        ALUGUEL = "aluguel", "Aluguel semanal"
        NOTA_DEBITO = "nota_debito", "Nota de débito"
        MANUTENCAO = "manutencao", "Repasse de manutenção"
        SINISTRO = "sinistro", "Repasse de sinistro"
        EXCEDENTE_KM = "excedente_km", "Excedente de km"
        ENCARGO_ATRASO = "encargo_atraso", "Encargo por atraso"

    class Status(models.TextChoices):
        PENDENTE = "pendente", "Pendente"
        PARCIAL = "parcial", "Parcial"
        PAGO = "pago", "Pago"
        ATRASADO = "atrasado", "Atrasado"
        JUDICIAL = "judicial", "Em cobrança judicial"

    # Classificação fiscal (docs.md §4.3): só o aluguel entra na base do DAS.
    ORIGENS_RECEITA_LOCACAO = {Origem.ALUGUEL}

    cliente = models.ForeignKey(
        Cliente, verbose_name="cliente", on_delete=models.PROTECT, related_name="cobrancas"
    )
    alocacao = models.ForeignKey(
        Alocacao,
        verbose_name="alocação",
        on_delete=models.PROTECT,
        related_name="cobrancas",
        null=True,
        blank=True,
    )
    origem = models.CharField("origem", max_length=20, choices=Origem.choices)
    descricao = models.CharField("descrição/referência", max_length=200, blank=True)
    valor = models.DecimalField("valor devido (R$)", max_digits=10, decimal_places=2)
    vencimento = models.DateField("vencimento")
    status = models.CharField(
        "status", max_length=12, choices=Status.choices, default=Status.PENDENTE
    )
    cobranca_origem = models.ForeignKey(
        "self",
        verbose_name="cobrança de origem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="encargos",
        help_text="Para encargos: a cobrança atrasada que os gerou",
    )
    observacoes = models.TextField("observações", blank=True)
    criado_em = models.DateTimeField("criado em", auto_now_add=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "cobrança"
        ordering = ["vencimento", "id"]

    def __str__(self):
        return f"{self.cliente.nome} — {self.get_origem_display()} — R$ {self.valor}"

    @property
    def total_pago(self):
        return self.baixas.aggregate(s=Sum("valor"))["s"] or Decimal("0")

    @property
    def saldo(self):
        return (self.valor - self.total_pago).quantize(CENTAVO)

    @property
    def entra_no_das(self):
        return self.origem in self.ORIGENS_RECEITA_LOCACAO

    @property
    def em_aberto(self):
        return self.status not in (self.Status.PAGO,)

    def atualizar_status(self, hoje=None):
        """Recalcula o status a partir do saldo e do vencimento."""
        if self.status == self.Status.JUDICIAL:
            return
        saldo = self.saldo
        if saldo <= 0:
            self.status = self.Status.PAGO
        elif self.total_pago > 0:
            self.status = self.Status.PARCIAL
        elif hoje is not None and self.vencimento < hoje:
            self.status = self.Status.ATRASADO
        else:
            self.status = self.Status.PENDENTE
        self.save(update_fields=["status"])


class Recebimento(models.Model):
    """Pagamento do cliente, distribuído entre cobranças (docs.md §4.3)."""

    class Forma(models.TextChoices):
        PIX = "pix", "Pix"
        DINHEIRO = "dinheiro", "Dinheiro"
        TRANSFERENCIA = "transferencia", "Transferência"
        CAUCAO = "caucao", "Abatido da caução"

    cliente = models.ForeignKey(
        Cliente, verbose_name="cliente", on_delete=models.PROTECT, related_name="recebimentos"
    )
    valor = models.DecimalField("valor recebido (R$)", max_digits=10, decimal_places=2)
    data = models.DateField("data do recebimento")
    forma = models.CharField("forma", max_length=15, choices=Forma.choices, default=Forma.PIX)
    observacoes = models.TextField("observações", blank=True)
    criado_em = models.DateTimeField("criado em", auto_now_add=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "recebimento"
        ordering = ["-data", "-id"]

    def __str__(self):
        return f"{self.cliente.nome} — R$ {self.valor} em {self.data:%d/%m/%Y}"

    @property
    def total_alocado(self):
        return self.baixas.aggregate(s=Sum("valor"))["s"] or Decimal("0")

    @property
    def saldo_nao_alocado(self):
        return (self.valor - self.total_alocado).quantize(CENTAVO)


class BaixaCobranca(models.Model):
    """Quanto de um recebimento quitou de uma cobrança (docs.md §4.3)."""

    recebimento = models.ForeignKey(
        Recebimento, verbose_name="recebimento", on_delete=models.CASCADE, related_name="baixas"
    )
    cobranca = models.ForeignKey(
        Cobranca, verbose_name="cobrança", on_delete=models.PROTECT, related_name="baixas"
    )
    valor = models.DecimalField("valor (R$)", max_digits=10, decimal_places=2)

    class Meta:
        verbose_name = "baixa de cobrança"
        verbose_name_plural = "baixas de cobrança"

    def __str__(self):
        return f"R$ {self.valor} → {self.cobranca}"


class NotaDebito(models.Model):
    """Cobrança numerada que agrupa repasses/multas (docs.md §4.3). Fora da base do DAS."""

    class Status(models.TextChoices):
        EMITIDA = "emitida", "Emitida"
        PAGA = "paga", "Paga"
        PARCIAL = "parcial", "Parcial"
        CANCELADA = "cancelada", "Cancelada"

    numero = models.PositiveIntegerField("número", unique=True, editable=False)
    cliente = models.ForeignKey(
        Cliente, verbose_name="cliente", on_delete=models.PROTECT, related_name="notas_debito"
    )
    data_emissao = models.DateField("data de emissão")
    status = models.CharField(
        "status", max_length=10, choices=Status.choices, default=Status.EMITIDA
    )
    cobranca = models.OneToOneField(
        Cobranca,
        verbose_name="cobrança gerada",
        on_delete=models.PROTECT,
        related_name="nota_debito",
        null=True,
        blank=True,
    )
    observacoes = models.TextField("observações", blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "nota de débito"
        verbose_name_plural = "notas de débito"
        ordering = ["-numero"]

    def __str__(self):
        return f"ND {self.numero:03d} — {self.cliente.nome}"

    def save(self, *args, **kwargs):
        if self.numero is None:
            ultimo = NotaDebito.objects.aggregate(m=models.Max("numero"))["m"] or 0
            self.numero = ultimo + 1
        super().save(*args, **kwargs)

    @property
    def total(self):
        return self.itens.aggregate(s=Sum("valor"))["s"] or Decimal("0")


class ItemNotaDebito(models.Model):
    """Linha de uma nota de débito (docs.md §4.3).

    Na etapa 5 as multas passam a ser incluídas aqui automaticamente.
    """

    nota = models.ForeignKey(
        NotaDebito, verbose_name="nota", on_delete=models.CASCADE, related_name="itens"
    )
    descricao = models.CharField("descrição", max_length=200)
    valor = models.DecimalField("valor (R$)", max_digits=10, decimal_places=2)

    class Meta:
        verbose_name = "item da nota de débito"
        verbose_name_plural = "itens da nota de débito"

    def __str__(self):
        return f"{self.descricao} — R$ {self.valor}"


class Caucao(models.Model):
    """Garantia opcional do cliente (docs.md §4.4). Pagamento diverso, fora do DAS."""

    class Status(models.TextChoices):
        RETIDA = "retida", "Retida"
        PARCIAL = "parcial", "Parcialmente utilizada"
        DEVOLVIDA = "devolvida", "Devolvida"

    cliente = models.ForeignKey(
        Cliente, verbose_name="cliente", on_delete=models.PROTECT, related_name="caucoes"
    )
    alocacao = models.ForeignKey(
        Alocacao,
        verbose_name="alocação",
        on_delete=models.PROTECT,
        related_name="caucoes",
        null=True,
        blank=True,
    )
    status = models.CharField(
        "status", max_length=10, choices=Status.choices, default=Status.RETIDA
    )
    observacoes = models.TextField("observações", blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "caução"
        verbose_name_plural = "cauções"

    def __str__(self):
        return f"Caução de {self.cliente.nome} — saldo R$ {self.saldo}"

    @property
    def saldo(self):
        entradas = self.movimentacoes.filter(tipo=MovimentacaoCaucao.Tipo.REFORCO).aggregate(
            s=Sum("valor")
        )["s"] or Decimal("0")
        saidas = self.movimentacoes.filter(
            tipo__in=[MovimentacaoCaucao.Tipo.DESCONTO, MovimentacaoCaucao.Tipo.DEVOLUCAO]
        ).aggregate(s=Sum("valor"))["s"] or Decimal("0")
        return (entradas - saidas).quantize(CENTAVO)


class MovimentacaoCaucao(models.Model):
    """Extrato da caução (docs.md §4.4)."""

    class Tipo(models.TextChoices):
        REFORCO = "reforco", "Reforço"
        DESCONTO = "desconto", "Desconto"
        DEVOLUCAO = "devolucao", "Devolução"

    caucao = models.ForeignKey(
        Caucao, verbose_name="caução", on_delete=models.CASCADE, related_name="movimentacoes"
    )
    tipo = models.CharField("tipo", max_length=10, choices=Tipo.choices)
    valor = models.DecimalField("valor (R$)", max_digits=8, decimal_places=2)
    data = models.DateField("data")
    origem = models.CharField(
        "origem", max_length=200, blank=True, help_text="Ex.: nº da ND ou manutenção"
    )
    observacoes = models.TextField("observações", blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "movimentação de caução"
        verbose_name_plural = "movimentações de caução"
        ordering = ["data", "id"]

    def __str__(self):
        return f"{self.get_tipo_display()} R$ {self.valor}"

    @transaction.atomic
    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        caucao = self.caucao
        if caucao.saldo <= 0:
            caucao.status = Caucao.Status.DEVOLVIDA
        elif caucao.movimentacoes.filter(
            tipo__in=[self.Tipo.DESCONTO, self.Tipo.DEVOLUCAO]
        ).exists():
            caucao.status = Caucao.Status.PARCIAL
        else:
            caucao.status = Caucao.Status.RETIDA
        caucao.save(update_fields=["status"])

    def clean(self):
        if self.valor is not None and self.valor <= 0:
            raise ValidationError({"valor": "O valor deve ser positivo."})
        if self.tipo == self.Tipo.DEVOLUCAO and self.caucao_id and self.valor:
            if self.valor > self.caucao.saldo:
                raise ValidationError(
                    {"valor": f"Saldo insuficiente (disponível: R$ {self.caucao.saldo})."}
                )
