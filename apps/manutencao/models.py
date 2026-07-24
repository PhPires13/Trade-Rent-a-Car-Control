from django.db import models
from simple_history.models import HistoricalRecords

from apps.frota.models import Veiculo


class ItemPreventiva(models.Model):
    """Item do plano de manutenção (docs.md §4.5).

    Com intervalo_km_padrao → preventiva por km (óleo, correia...).
    Sem intervalo → manutenção esporádica (suspensão, embreagem, velas).
    A lista é aberta: novos itens podem ser cadastrados a qualquer momento.
    """

    nome = models.CharField("nome", max_length=80, unique=True)
    intervalo_km_padrao = models.PositiveIntegerField(
        "intervalo padrão (km)",
        null=True,
        blank=True,
        help_text="Vazio = manutenção esporádica (sem ciclo por km)",
    )
    ativo = models.BooleanField("ativo", default=True)
    observacoes = models.TextField("observações", blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "item de manutenção"
        verbose_name_plural = "itens de manutenção"
        ordering = ["nome"]

    def __str__(self):
        return self.nome

    @property
    def eh_preventiva(self):
        return self.intervalo_km_padrao is not None


class IntervaloPersonalizado(models.Model):
    """Intervalo de km específico de um veículo para um item (docs.md §4.5).

    Ex.: pneus a cada 20.000 km num carro de rodagem pesada (padrão 30.000).
    """

    veiculo = models.ForeignKey(
        Veiculo,
        verbose_name="veículo",
        on_delete=models.CASCADE,
        related_name="intervalos_personalizados",
    )
    item = models.ForeignKey(
        ItemPreventiva, verbose_name="item", on_delete=models.CASCADE, related_name="intervalos"
    )
    intervalo_km = models.PositiveIntegerField("intervalo (km)")

    history = HistoricalRecords()

    class Meta:
        verbose_name = "intervalo personalizado"
        verbose_name_plural = "intervalos personalizados"
        constraints = [
            models.UniqueConstraint(fields=["veiculo", "item"], name="um_intervalo_por_item")
        ]

    def __str__(self):
        return f"{self.veiculo.placa} — {self.item.nome}: {self.intervalo_km} km"


class Manutencao(models.Model):
    """Manutenção realizada num veículo (docs.md §4.5).

    Registra a execução (zera o ciclo da preventiva) e o lado financeiro:
    custo real × valor cobrado do cliente, dias parado e vínculo com sinistro.
    """

    class Tipo(models.TextChoices):
        PREVENTIVA = "preventiva", "Preventiva"
        CORRETIVA = "corretiva", "Corretiva"
        ESPORADICA = "esporadica", "Esporádica"

    class OrigemCusto(models.TextChoices):
        PROTECAO = "protecao", "Evento da proteção (Auto Truck)"
        PARTICULAR = "particular", "Particular (por fora)"

    class Responsavel(models.TextChoices):
        EMPRESA = "empresa", "Empresa"
        CLIENTE = "cliente", "Cliente"

    veiculo = models.ForeignKey(
        Veiculo, verbose_name="veículo", on_delete=models.PROTECT, related_name="manutencoes"
    )
    item = models.ForeignKey(
        ItemPreventiva,
        verbose_name="item do plano",
        on_delete=models.PROTECT,
        related_name="manutencoes",
        null=True,
        blank=True,
        help_text="Preencher quando a manutenção corresponde a um item do plano (zera o ciclo)",
    )
    tipo = models.CharField("tipo", max_length=15, choices=Tipo.choices)
    data = models.DateField("data")
    km = models.PositiveIntegerField("KM na execução", null=True, blank=True)
    descricao = models.TextField(
        "descrição do reparo",
        help_text="Descreva o serviço feito — pesa na avaliação de desmobilização (§4.9)",
    )
    oficina = models.ForeignKey(
        "frota.Fornecedor",
        verbose_name="oficina/fornecedor",
        on_delete=models.PROTECT,
        related_name="manutencoes",
        null=True,
        blank=True,
    )
    data_entrada = models.DateField(
        "data de entrada na oficina",
        null=True,
        blank=True,
        help_text="Preencher para acompanhar os dias parado (auxílio motorista >7 dias)",
    )
    data_saida = models.DateField("data de saída", null=True, blank=True)
    origem_custo = models.CharField(
        "origem do custo",
        max_length=12,
        choices=OrigemCusto.choices,
        default=OrigemCusto.PARTICULAR,
    )
    custo_real = models.DecimalField(
        "custo real (R$)",
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Pago à oficina/associação; zero se coberto por franquia gratuita",
    )
    valor_cobrado_cliente = models.DecimalField(
        "valor cobrado do cliente (R$)",
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Pode ser diferente do custo real (maior ou menor)",
    )
    responsavel = models.CharField(
        "responsável pelo custo",
        max_length=10,
        choices=Responsavel.choices,
        default=Responsavel.EMPRESA,
    )
    pagamento_custo = models.CharField(
        "pagamento à oficina",
        max_length=10,
        choices=[("pendente", "Pendente"), ("pago", "Pago")],
        default="pendente",
    )
    sinistro = models.ForeignKey(
        "sinistros.Sinistro",
        verbose_name="sinistro",
        on_delete=models.PROTECT,
        related_name="manutencoes",
        null=True,
        blank=True,
    )
    cobranca_repasse = models.OneToOneField(
        "financeiro.Cobranca",
        verbose_name="cobrança de repasse",
        on_delete=models.PROTECT,
        related_name="manutencao_repassada",
        null=True,
        blank=True,
    )
    observacoes = models.TextField("observações", blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "manutenção"
        verbose_name_plural = "manutenções"
        ordering = ["-data"]

    def __str__(self):
        rotulo = self.item.nome if self.item else self.get_tipo_display()
        return f"{self.veiculo.placa} — {rotulo} em {self.data:%d/%m/%Y}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.km and self.km > self.veiculo.km_atual:
            self.veiculo.km_atual = self.km
            self.veiculo.save(update_fields=["km_atual"])

    @property
    def dias_parado(self):
        if not self.data_entrada:
            return 0
        from datetime import date

        fim = self.data_saida or date.today()
        return (fim - self.data_entrada).days

    @property
    def diferenca(self):
        """Valor cobrado − custo real: quanto a empresa ganhou ou absorveu (docs.md §4.5)."""
        if self.valor_cobrado_cliente is None or self.custo_real is None:
            return None
        return self.valor_cobrado_cliente - self.custo_real
