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

    #: Campos que definem o encadeamento entre os meses — mexer em qualquer um
    #: deles obriga a recalcular KM ANT/dias deste registro e do mês seguinte.
    CAMPOS_DE_ENCADEAMENTO = frozenset({"km", "data_leitura", "mes_referencia"})

    #: Preenchido pelo save() com o registro do mês seguinte que teve de ser
    #: recalculado (a tela avisa quem já tinha cobrança de excedente emitida).
    seguinte_reencadeado = None

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
        update_fields = kwargs.get("update_fields")
        # Gravação parcial que não toca na cadeia (ex.: vincular a cobrança de
        # excedente) não precisa recalcular nem mexer no mês seguinte.
        encadear = update_fields is None or bool(
            self.CAMPOS_DE_ENCADEAMENTO.intersection(update_fields)
        )
        if encadear:
            self.km_anterior, self.dias = self._encadeamento_com_o_anterior()
            if update_fields is not None:
                kwargs["update_fields"] = list(set(update_fields) | {"km_anterior", "dias"})
        super().save(*args, **kwargs)
        if self.km > self.veiculo.km_atual:
            self.veiculo.km_atual = self.km
            self.veiculo.save(update_fields=["km_atual"])
        if encadear:
            # Lançamento retroativo ou edição pelo admin entram no meio da
            # cadeia: o mês seguinte fica com KM ANT/dias da leitura errada.
            self.seguinte_reencadeado = self._reencadear_seguinte()

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

    def registro_seguinte(self):
        """Primeira leitura do mesmo veículo depois deste mês, se já existir."""
        return (
            RegistroKm.objects.filter(veiculo=self.veiculo, mes_referencia__gt=self.mes_referencia)
            .order_by("mes_referencia")
            .first()
        )

    def _encadeamento_com_o_anterior(self):
        """(KM ANT, dias) desta leitura — da leitura anterior ou do km de compra."""
        anterior = self.registro_anterior()
        if anterior is None:
            return self.veiculo.km_compra, None
        return anterior.km, (self.data_leitura - anterior.data_leitura).days

    def _reencadear_seguinte(self):
        """Reapoia a leitura do mês seguinte nesta. Devolve-a se algo mudou."""
        seguinte = self.registro_seguinte()
        if seguinte is None:
            return None
        dias = (seguinte.data_leitura - self.data_leitura).days
        if seguinte.km_anterior == self.km and seguinte.dias == dias:
            return None
        seguinte.km_anterior = self.km
        seguinte.dias = dias
        # update_fields sem campo de encadeamento: não recalcula nem propaga de
        # novo (a leitura depois dela não depende do KM ANT/dias desta).
        seguinte.save(update_fields=["km_anterior", "dias"])
        return seguinte

    def clean(self):
        if self.veiculo_id is None or self.mes_referencia is None or self.km is None:
            return  # campo obrigatório em falta — clean_fields() já acusou
        self.mes_referencia = primeiro_dia_do_mes(self.mes_referencia)
        anterior = self.registro_anterior()
        if anterior is not None and self.km < anterior.km:
            raise ValidationError(
                {"km": f"KM ({self.km}) menor que a leitura anterior ({anterior.km})."}
            )
        seguinte = self.registro_seguinte()
        if seguinte is not None and self.km > seguinte.km:
            # Mês esquecido lançado depois: o odômetro não pode ficar maior que
            # o da leitura que já está gravada mais adiante.
            raise ValidationError(
                {
                    "km": (
                        f"KM ({self.km}) maior que a leitura de "
                        f"{seguinte.mes_referencia:%m/%Y} ({seguinte.km}), já registrada."
                    )
                }
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
