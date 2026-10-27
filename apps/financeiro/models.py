from decimal import Decimal

from django.db import models
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from simple_history.models import HistoricalRecords

from apps.alocacoes.models import Alocacao
from apps.formatos import brl
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
        REPASSE_MANUTENCAO = "repasse_manutencao", "Conserto cobrado do cliente"
        REPASSE_SINISTRO = "repasse_sinistro", "Franquia de sinistro cobrada do cliente"
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
    # Dias de aluguel que a cobrança paga. Quase sempre [vencimento, vencimento+6];
    # muda na 1ª semana proporcional (entrega fora do dia de pagar) e na troca do
    # dia de vencimento. Vazio (cobranças antigas) = a semana a partir do vencimento.
    periodo_inicio = models.DateField("aluguel de", null=True, blank=True)
    periodo_fim = models.DateField("aluguel até", null=True, blank=True)
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
        return f"{self.descricao} — {self.cliente.nome} ({brl(self.valor)})"

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
        Só grava quando o status muda — a rotina diária passa por todas as
        cobranças em aberto e não deve gerar versão repetida no histórico.
        """
        hoje = hoje or timezone.localdate()
        # as aplicações podem ter mudado desde o fetch — não confiar em anotação
        self.__dict__.pop("_total_quitado_anotado", None)
        if self.status == self.Status.CANCELADA:
            return
        quitado = self.total_quitado  # calculado uma vez (2 consultas)
        if quitado >= self.valor:
            novo = self.Status.PAGO
        elif self.status == self.Status.JUDICIAL:
            return
        elif self.vencimento < hoje:
            novo = self.Status.ATRASADO
        elif quitado > ZERO:
            novo = self.Status.PARCIAL
        else:
            novo = self.Status.PENDENTE
        if novo != self.status:
            self.status = novo
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
        return f"{brl(self.valor)} de {self.cliente.nome} em {self.data:%d/%m/%Y}"


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
        return f"{brl(self.valor)} → {self.cobranca.descricao}"


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
        return f"{self.get_tipo_display()}: {brl(self.valor)} — {self.cliente.nome}"

    @classmethod
    def saldo_do_cliente(cls, cliente):
        """Entradas − usos, numa consulta só."""
        somas = cliente.creditos.aggregate(
            entradas=Sum("valor", filter=Q(tipo=cls.Tipo.ENTRADA)),
            usos=Sum("valor", filter=Q(tipo=cls.Tipo.USO)),
        )
        return (somas["entradas"] or ZERO) - (somas["usos"] or ZERO)


class NotaDebito(models.Model):
    """ND — cobrança numerada que agrupa repasses do motorista (docs.md §4.3).

    Numeração automática dando sequência à atual; multas repassadas entram
    como itens (ItemNotaDebito.multa).
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
        # lista de NDs usa prefetch_related("itens"): soma em memória, sem query por linha
        if "itens" in getattr(self, "_prefetched_objects_cache", {}):
            return sum((item.valor for item in self.itens.all()), ZERO)
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
        return f"{self.descricao} ({brl(self.valor)})"


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
        return f"{self.get_tipo_display()} {brl(self.valor)} — {self.caucao}"
