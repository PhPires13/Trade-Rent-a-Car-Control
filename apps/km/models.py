from django.core.exceptions import ValidationError
from django.db import models
from simple_history.models import HistoricalRecords

from apps.frota.models import Veiculo


def primeiro_dia_do_mes(data):
    return data.replace(day=1)


class RegistroKm(models.Model):
    """Registro mensal de KM por veículo (docs.md §4.8) — um por veículo por mês.

    KM ANT e dias são gravados no momento do registro (como na planilha),
    a partir do registro do mês anterior.
    """

    veiculo = models.ForeignKey(
        Veiculo, verbose_name="veículo", on_delete=models.PROTECT, related_name="registros_km"
    )
    mes_referencia = models.DateField("mês de referência", help_text="Sempre dia 1º do mês")
    data_leitura = models.DateField("data da leitura")
    km = models.PositiveIntegerField("KM (odômetro)")
    km_anterior = models.PositiveIntegerField("KM anterior", null=True, blank=True)
    dias = models.PositiveIntegerField("dias desde a leitura anterior", null=True, blank=True)
    cobranca_excedente = models.OneToOneField(
        "financeiro.Cobranca",
        verbose_name="cobrança de excedente de km",
        # PROTECT: apagar a cobrança desfaria a idempotência e a rotina diária
        # a recriaria — quem decidir não cobrar deve cancelá-la no Financeiro
        on_delete=models.PROTECT,
        related_name="registro_km_excedente",
        null=True,
        blank=True,
        help_text="Gerada automaticamente quando a alocação limitada estoura a franquia",
    )
    observacoes = models.TextField("observações", blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "registro de KM"
        verbose_name_plural = "registros de KM"
        constraints = [
            models.UniqueConstraint(
                fields=["veiculo", "mes_referencia"], name="um_registro_por_veiculo_por_mes"
            )
        ]
        ordering = ["-mes_referencia"]

    def __str__(self):
        return f"{self.veiculo.placa} — {self.mes_referencia:%m/%Y}: {self.km} km"

    def save(self, *args, **kwargs):
        self.mes_referencia = primeiro_dia_do_mes(self.mes_referencia)
        if self._state.adding:
            anterior = self.registro_anterior()
            if anterior:
                self.km_anterior = anterior.km
                self.dias = (self.data_leitura - anterior.data_leitura).days
            else:
                self.km_anterior = self.veiculo.km_compra
        super().save(*args, **kwargs)
        if self.km > self.veiculo.km_atual:
            self.veiculo.km_atual = self.km
            self.veiculo.save(update_fields=["km_atual"])

    @property
    def km_utilizado(self):
        if self.km_anterior is None:
            return None
        return self.km - self.km_anterior

    @property
    def media_dia(self):
        if not self.dias or self.km_utilizado is None:
            return None
        return self.km_utilizado / self.dias

    @property
    def media_mes(self):
        if self.media_dia is None:
            return None
        return self.media_dia * 30

    def registro_anterior(self):
        return (
            RegistroKm.objects.filter(veiculo=self.veiculo, mes_referencia__lt=self.mes_referencia)
            .order_by("-mes_referencia")
            .first()
        )

    def clean(self):
        anterior = self.registro_anterior()
        referencia = anterior.km if anterior else None
        if referencia is not None and self.km < referencia:
            raise ValidationError(
                {"km": f"KM ({self.km}) menor que a leitura anterior ({referencia})."}
            )


def veiculos_com_leitura_pendente(mes):
    """Veículos de locação ativos sem registro no mês de referência (docs.md §4.8)."""
    mes = primeiro_dia_do_mes(mes)
    return (
        Veiculo.objects.filter(uso=Veiculo.Uso.LOCACAO)
        .exclude(status__in=[Veiculo.Status.VENDIDO, Veiculo.Status.INATIVO])
        .exclude(registros_km__mes_referencia=mes)
        .order_by("placa")
    )
