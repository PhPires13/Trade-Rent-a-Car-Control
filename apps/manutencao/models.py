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

    Nesta etapa registra a execução (zera o ciclo da preventiva);
    os campos financeiros (custo real × cobrado, oficina, dias parado)
    entram na etapa 5.
    """

    class Tipo(models.TextChoices):
        PREVENTIVA = "preventiva", "Preventiva"
        CORRETIVA = "corretiva", "Corretiva"
        ESPORADICA = "esporadica", "Esporádica"

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
