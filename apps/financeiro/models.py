from decimal import Decimal

from django.db import models
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce
from simple_history.models import HistoricalRecords

from apps.alocacoes.models import Alocacao
from apps.pessoas.models import Cliente

ZERO = Decimal("0.00")


class Cobranca(models.Model):
    """Valor devido por um cliente (docs.md §4.3).

    A classificação fiscal deriva da origem: aluguel → receita de locação
    (fatura, base do DAS); todo o resto → pagamento diverso (nota de débito,
    fora da base do DAS).
    """

    class Origem(models.TextChoices):
        ALUGUEL = "aluguel", "Aluguel semanal"
        NOTA_DEBITO = "nota_debito", "Nota de débito"
        REPASSE_MANUTENCAO = "repasse_manutencao", "Repasse de manutenção"
        REPASSE_SINISTRO = "repasse_sinistro", "Repasse de sinistro"
        EXCEDENTE_KM = "excedente_km", "Excedente de km"
        ENCARGO = "encargo", "Encargo por atraso"
        OUTRO = "outro", "Outro"

    class Status(models.TextChoices):
        PENDENTE = "pendente", "Pendente"
        PARCIAL = "parcial", "Parcial"
        PAGO = "pago", "Pago"
        ATRASADO = "atrasado", "Atrasado"
        JUDICIAL = "judicial", "Em cobrança judicial"
        CANCELADA = "cancelada", "Cancelada"

    #: Fonte única dos conjuntos de status usados nas telas e relatórios.
    #: EM_ABERTO segue o fluxo normal de cobrança (encargo, atraso);
    #: DEVIDOS é tudo que o cliente ainda deve — em aberto + judicial —
    #: e é o que pode ser recebido ou descontado da caução.
    STATUS_EM_ABERTO = [Status.PENDENTE, Status.PARCIAL, Status.ATRASADO]
    STATUS_DEVIDOS = [*STATUS_EM_ABERTO, Status.JUDICIAL]

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
    descricao = models.CharField("descrição", max_length=200)
    valor = models.DecimalField("valor (R$)", max_digits=10, decimal_places=2)
    vencimento = models.DateField("vencimento")
    status = models.CharField(
        "status", max_length=12, choices=Status.choices, default=Status.PENDENTE
    )
    nota_debito = models.OneToOneField(
        "NotaDebito",
        verbose_name="nota de débito",
        on_delete=models.PROTECT,
        related_name="cobranca",
        null=True,
        blank=True,
    )
    cobranca_origem = models.ForeignKey(
        "self",
        verbose_name="cobrança que originou (encargo)",
        on_delete=models.PROTECT,
        related_name="encargos",
        null=True,
        blank=True,
    )
    observacoes = models.TextField("observações", blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "cobrança"
        ordering = ["vencimento", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["alocacao", "vencimento"],
                condition=models.Q(origem="aluguel"),
                name="um_aluguel_por_alocacao_por_vencimento",
            )
        ]

    def __str__(self):
        return f"{self.descricao} — {self.cliente.nome} (R$ {self.valor})"

    @classmethod
    def com_quitacao_anotada(cls, queryset=None):
        """Anota o total quitado em lote — telas de lista sem 2 queries por linha.

        Subqueries em vez de joins: somar dois reverse FKs (aplicações e
        descontos de caução) num annotate só multiplicaria as linhas.
        `total_quitado`/`saldo` usam o valor anotado quando presente.
        """
        dinheiro = models.DecimalField(max_digits=10, decimal_places=2)
        recebido = (
            AplicacaoRecebimento.objects.filter(cobranca=models.OuterRef("pk"))
            .values("cobranca")
            .annotate(t=Sum("valor"))
            .values("t")
        )
        caucao = (
            MovimentacaoCaucao.objects.filter(cobranca=models.OuterRef("pk"))
            .values("cobranca")
            .annotate(t=Sum("valor"))
            .values("t")
        )
        queryset = queryset if queryset is not None else cls.objects.all()
        return queryset.annotate(
            _total_quitado_anotado=Coalesce(
                models.Subquery(recebido), models.Value(ZERO), output_field=dinheiro
            )
            + Coalesce(models.Subquery(caucao), models.Value(ZERO), output_field=dinheiro)
        )

    @property
    def total_quitado(self):
        anotado = getattr(self, "_total_quitado_anotado", None)
        if anotado is not None:
            return anotado
        recebido = self.aplicacoes.aggregate(t=Sum("valor"))["t"] or ZERO
        caucao = self.descontos_caucao.aggregate(t=Sum("valor"))["t"] or ZERO
        return recebido + caucao

    @property
    def saldo(self):
        return self.valor - self.total_quitado

    @property
    def classificacao_fiscal(self):
        """'locacao' entra na base do DAS (fatura); 'diverso' não (nota de débito)."""
        return "locacao" if self.origem == self.Origem.ALUGUEL else "diverso"

    def atualizar_status(self, hoje=None):
        """Recalcula o status a partir do saldo e do vencimento (docs.md §4.3).

        Judicial congela o fluxo normal, mas quitação total vira PAGO —
        senão a cobrança judicial recebida ficaria devendo para sempre.
        A volta ao fluxo normal é decisão do dono (view reabrir_judicial).
        """
        from datetime import date

        hoje = hoje or date.today()
        # as aplicações podem ter mudado desde o fetch — não confiar em anotação
        self.__dict__.pop("_total_quitado_anotado", None)
        if self.status == self.Status.CANCELADA:
            return
        if self.status == self.Status.JUDICIAL:
            if self.saldo <= ZERO:
                self.status = self.Status.PAGO
                self.save(update_fields=["status"])
            return
        if self.saldo <= ZERO:
            self.status = self.Status.PAGO
        elif self.total_quitado > ZERO:
            self.status = self.Status.ATRASADO if self.vencimento < hoje else self.Status.PARCIAL
        else:
            self.status = self.Status.ATRASADO if self.vencimento < hoje else self.Status.PENDENTE
        self.save(update_fields=["status"])


class Recebimento(models.Model):
    """Pagamento do cliente, aplicado em uma ou mais cobranças (docs.md §4.3)."""

    class Forma(models.TextChoices):
        PIX = "pix", "Pix"
        DINHEIRO = "dinheiro", "Dinheiro"
        TRANSFERENCIA = "transferencia", "Transferência"
        CREDITO = "credito", "Crédito do cliente"

    cliente = models.ForeignKey(
        Cliente, verbose_name="cliente", on_delete=models.PROTECT, related_name="recebimentos"
    )
    data = models.DateField("data do recebimento")
    valor = models.DecimalField("valor recebido (R$)", max_digits=10, decimal_places=2)
    forma = models.CharField("forma", max_length=15, choices=Forma.choices, default=Forma.PIX)
    observacoes = models.TextField("observações", blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "recebimento"
        ordering = ["-data", "-pk"]

    def __str__(self):
        return f"R$ {self.valor} de {self.cliente.nome} em {self.data:%d/%m/%Y}"

    @property
    def total_aplicado(self):
        return self.aplicacoes.aggregate(t=Sum("valor"))["t"] or ZERO


class AplicacaoRecebimento(models.Model):
    """Parcela de um recebimento aplicada numa cobrança específica."""

    recebimento = models.ForeignKey(
        Recebimento, on_delete=models.CASCADE, related_name="aplicacoes"
    )
    cobranca = models.ForeignKey(Cobranca, on_delete=models.PROTECT, related_name="aplicacoes")
    valor = models.DecimalField("valor aplicado (R$)", max_digits=10, decimal_places=2)

    class Meta:
        verbose_name = "aplicação de recebimento"
        verbose_name_plural = "aplicações de recebimento"

    def __str__(self):
        return f"R$ {self.valor} → {self.cobranca.descricao}"


class MovimentoCredito(models.Model):
    """Crédito/adiantamento do cliente (sobra de recebimento) e seus usos (docs.md §4.3)."""

    class Tipo(models.TextChoices):
        ENTRADA = "entrada", "Entrada (sobra de pagamento)"
        USO = "uso", "Uso (abatimento em cobrança)"

    cliente = models.ForeignKey(
        Cliente, verbose_name="cliente", on_delete=models.PROTECT, related_name="creditos"
    )
    tipo = models.CharField("tipo", max_length=10, choices=Tipo.choices)
    valor = models.DecimalField("valor (R$)", max_digits=10, decimal_places=2)
    data = models.DateField("data")
    recebimento = models.ForeignKey(
        Recebimento, on_delete=models.PROTECT, related_name="movimentos_credito", null=True
    )
    observacoes = models.TextField("observações", blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "movimento de crédito"
        verbose_name_plural = "movimentos de crédito"
        ordering = ["-data", "-pk"]

    def __str__(self):
        return f"{self.get_tipo_display()}: R$ {self.valor} — {self.cliente.nome}"

    @staticmethod
    def saldo_do_cliente(cliente):
        entradas = cliente.creditos.filter(tipo="entrada").aggregate(t=Sum("valor"))["t"] or ZERO
        usos = cliente.creditos.filter(tipo="uso").aggregate(t=Sum("valor"))["t"] or ZERO
        return entradas - usos


class NotaDebito(models.Model):
    """ND — cobrança numerada que agrupa repasses do motorista (docs.md §4.3).

    Numeração automática dando sequência à atual; na etapa 5 as multas
    entram como itens.
    """

    numero = models.PositiveIntegerField("número", unique=True)
    cliente = models.ForeignKey(
        Cliente, verbose_name="cliente", on_delete=models.PROTECT, related_name="notas_debito"
    )
    data_emissao = models.DateField("data de emissão")
    vencimento = models.DateField("vencimento", null=True, blank=True)
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
            maior = NotaDebito.objects.aggregate(m=models.Max("numero"))["m"] or 0
            self.numero = maior + 1
        super().save(*args, **kwargs)

    @property
    def total(self):
        return self.itens.aggregate(t=Sum("valor"))["t"] or ZERO


class ItemNotaDebito(models.Model):
    """Item de uma ND — multa vinculada, avaria, excedente ou descrição livre."""

    nota_debito = models.ForeignKey(NotaDebito, on_delete=models.CASCADE, related_name="itens")
    descricao = models.CharField("descrição", max_length=200)
    valor = models.DecimalField("valor (R$)", max_digits=10, decimal_places=2)
    multa = models.ForeignKey(
        "multas.Multa",
        verbose_name="multa",
        on_delete=models.PROTECT,
        related_name="itens_nd",
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = "item da nota de débito"
        verbose_name_plural = "itens da nota de débito"

    def __str__(self):
        return f"{self.descricao} (R$ {self.valor})"


class Caucao(models.Model):
    """Caução de uma alocação — opcional (docs.md §4.4). Saldo vem das movimentações."""

    alocacao = models.OneToOneField(
        Alocacao, verbose_name="alocação", on_delete=models.PROTECT, related_name="caucao"
    )
    observacoes = models.TextField("observações", blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "caução"
        verbose_name_plural = "cauções"

    def __str__(self):
        return f"Caução de {self.alocacao.cliente.nome} ({self.alocacao.veiculo.placa})"

    @classmethod
    def com_saldos_anotados(cls, queryset=None):
        """Recebido/descontado/devolvido agregados em lote (padrão dos hubs).

        Sem isso, cada linha da tela de cauções dispara ~12 queries entre
        as properties; com a anotação a lista inteira sai em uma consulta.
        """
        dinheiro = models.DecimalField(max_digits=10, decimal_places=2)

        def soma(*tipos):
            return Coalesce(
                Sum("movimentacoes__valor", filter=Q(movimentacoes__tipo__in=tipos)),
                models.Value(ZERO),
                output_field=dinheiro,
            )

        queryset = queryset if queryset is not None else cls.objects.all()
        return queryset.annotate(
            _recebido_anotado=soma("recebimento", "reforco"),
            _descontado_anotado=soma("desconto"),
            _devolvido_anotado=soma("devolucao"),
        )

    def _soma(self, anotacao, *tipos):
        anotado = getattr(self, anotacao, None)
        if anotado is not None:
            return anotado
        return self.movimentacoes.filter(tipo__in=tipos).aggregate(t=Sum("valor"))["t"] or ZERO

    @property
    def recebido(self):
        return self._soma("_recebido_anotado", "recebimento", "reforco")

    @property
    def descontado(self):
        return self._soma("_descontado_anotado", "desconto")

    @property
    def devolvido(self):
        return self._soma("_devolvido_anotado", "devolucao")

    @property
    def saldo(self):
        return self.recebido - self.descontado - self.devolvido

    @property
    def status(self):
        if self.recebido == ZERO:
            return "Sem depósito"
        if self.saldo == ZERO and self.devolvido > ZERO:
            return "Devolvida"
        if self.descontado > ZERO or self.devolvido > ZERO:
            return "Parcialmente utilizada"
        return "Retida"


class MovimentacaoCaucao(models.Model):
    """Lançamento no extrato da caução (docs.md §4.4)."""

    class Tipo(models.TextChoices):
        RECEBIMENTO = "recebimento", "Recebimento"
        REFORCO = "reforco", "Reforço"
        DESCONTO = "desconto", "Desconto"
        DEVOLUCAO = "devolucao", "Devolução"

    caucao = models.ForeignKey(Caucao, on_delete=models.CASCADE, related_name="movimentacoes")
    tipo = models.CharField("tipo", max_length=15, choices=Tipo.choices)
    valor = models.DecimalField("valor (R$)", max_digits=10, decimal_places=2)
    data = models.DateField("data")
    forma = models.CharField(
        "forma", max_length=15, choices=Recebimento.Forma.choices[:3], blank=True
    )
    cobranca = models.ForeignKey(
        Cobranca,
        verbose_name="cobrança quitada (descontos)",
        on_delete=models.PROTECT,
        related_name="descontos_caucao",
        null=True,
        blank=True,
    )
    observacoes = models.TextField("observações", blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "movimentação de caução"
        verbose_name_plural = "movimentações de caução"
        ordering = ["-data", "-pk"]

    def __str__(self):
        return f"{self.get_tipo_display()} R$ {self.valor} — {self.caucao}"
