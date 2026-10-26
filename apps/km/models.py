from dataclasses import dataclass
from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Min
from simple_history.models import HistoricalRecords

from apps.formatos import milhar
from apps.frota.models import Veiculo


def primeiro_dia_do_mes(data):
    return data.replace(day=1)


def mes_anterior(mes):
    """1º do mês anterior ao mês de `mes`."""
    return primeiro_dia_do_mes(primeiro_dia_do_mes(mes) - timedelta(days=1))


#: Leitura de "fechamento" anotada nos primeiros dias do mês seguinte ainda
#: pode valer para o mês que ela fecha (decisão D21).
DIAS_DE_FECHAMENTO = 5
#: Quantos meses (contando o atual) o painel cobra de leitura esquecida (D21).
MESES_COBRADOS_NO_PAINEL = 3


def leitura_cabe_no_mes(data_leitura, mes):
    """A leitura dessa data pode ser a do mês `mes`?

    Sim no próprio mês e, para fechar o mês anterior, nos primeiros
    DIAS_DE_FECHAMENTO dias do mês seguinte (leitura feita no dia 1º).
    """
    mes = primeiro_dia_do_mes(mes)
    if primeiro_dia_do_mes(data_leitura) == mes:
        return True
    return data_leitura.day <= DIAS_DE_FECHAMENTO and mes_anterior(data_leitura) == mes


def mes_da_leitura(data_leitura, mes_escolhido=None):
    """Mês de referência da leitura: o mês da data, ou o mês escolhido na tela
    quando a leitura é o fechamento dele feito no começo do mês seguinte."""
    if mes_escolhido is not None and leitura_cabe_no_mes(data_leitura, mes_escolhido):
        return primeiro_dia_do_mes(mes_escolhido)
    return primeiro_dia_do_mes(data_leitura)


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
        # Leitura, KM atual do carro e mês seguinte reencadeado: tudo ou nada.
        with transaction.atomic():
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
        """(KM ANT, dias) desta leitura — da leitura anterior ou do km de compra.

        Primeira leitura: o km de compra só serve de KM ANT se o carro foi
        comprado há pouco (no mês da leitura ou no anterior). Carro comprado há
        tempos entra com a 1ª leitura como ponto de partida, sem "KM rodado" —
        senão anos de uso contariam como rodados agora (decisão D20).
        """
        anterior = self.registro_anterior()
        if anterior is None:
            if self._compra_recente():
                return self.veiculo.km_compra, None
            return None, None
        return anterior.km, (self.data_leitura - anterior.data_leitura).days

    def _compra_recente(self):
        veiculo = self.veiculo
        if veiculo.km_compra is None or veiculo.data_aquisicao is None:
            return False
        return veiculo.data_aquisicao >= mes_anterior(self.mes_referencia)

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
        seguinte = self.registro_seguinte()
        if self.data_leitura is not None:
            # Com a leitura de fechamento (D21) a data pode cair no mês seguinte:
            # a ordem das datas precisa continuar a mesma dos meses.
            if anterior is not None and self.data_leitura < anterior.data_leitura:
                raise ValidationError(
                    {
                        "data_leitura": (
                            f"A data precisa ser depois da leitura de "
                            f"{anterior.mes_referencia:%m/%Y} "
                            f"({anterior.data_leitura:%d/%m/%Y})."
                        )
                    }
                )
            if seguinte is not None and self.data_leitura > seguinte.data_leitura:
                raise ValidationError(
                    {
                        "data_leitura": (
                            f"A data precisa ser antes da leitura de "
                            f"{seguinte.mes_referencia:%m/%Y} "
                            f"({seguinte.data_leitura:%d/%m/%Y})."
                        )
                    }
                )
        if anterior is not None and self.km < anterior.km:
            raise ValidationError(
                {
                    "km": (
                        f"{milhar(self.km)} km é menor que a leitura anterior "
                        f"({milhar(anterior.km)} km em {anterior.data_leitura:%d/%m/%Y}) — "
                        f"o odômetro não volta. Confira o número."
                    )
                }
            )
        if seguinte is not None and self.km > seguinte.km:
            # Mês esquecido lançado depois: o odômetro não pode ficar maior que
            # o da leitura que já está gravada mais adiante.
            raise ValidationError(
                {
                    "km": (
                        f"{milhar(self.km)} km é maior que a leitura de "
                        f"{seguinte.mes_referencia:%m/%Y} ({milhar(seguinte.km)} km), "
                        f"já registrada."
                    )
                }
            )


class AcertoExcedenteKm(models.Model):
    """Excedente de um cliente que devolveu o carro no meio do período da leitura.

    A leitura tem uma cobrança "principal" (cobranca_excedente, de quem estava
    com o carro por último). Quando outro contrato limitado usou o mesmo carro
    antes, no mesmo período, o excedente dele vira uma cobrança à parte aqui —
    senão o km a mais do primeiro cliente se perdia (bug C15).
    """

    registro = models.ForeignKey(
        RegistroKm, on_delete=models.CASCADE, related_name="acertos_excedente"
    )
    alocacao = models.ForeignKey(
        "alocacoes.Alocacao", on_delete=models.PROTECT, related_name="acertos_excedente_km"
    )
    cobranca = models.OneToOneField(
        "financeiro.Cobranca", on_delete=models.PROTECT, related_name="acerto_excedente_km"
    )

    class Meta:
        verbose_name = "acerto de excedente de km"
        verbose_name_plural = "acertos de excedente de km"
        constraints = [
            models.UniqueConstraint(
                fields=["registro", "alocacao"], name="um_acerto_por_alocacao_por_leitura"
            )
        ]

    def __str__(self):
        return f"{self.registro} — {self.cobranca}"


def veiculos_com_leitura_pendente(mes):
    """Veículos de locação ativos sem registro no mês de referência (docs.md §4.8).

    Mês anterior à compra não conta (o carro ainda não era da empresa), como em
    pendencias_de_leitura. Já o mês anterior à primeira leitura continua na
    lista — é por ela que se lançam leituras antigas na carga de dados —, mas o
    painel não cobra esses meses.
    """
    mes = primeiro_dia_do_mes(mes)
    mes_seguinte = (mes + timedelta(days=32)).replace(day=1)
    return (
        Veiculo.objects.filter(uso=Veiculo.Uso.LOCACAO)
        .exclude(status__in=[Veiculo.Status.VENDIDO, Veiculo.Status.INATIVO])
        .exclude(registros_km__mes_referencia=mes)
        .filter(models.Q(data_aquisicao__isnull=True) | models.Q(data_aquisicao__lt=mes_seguinte))
        .order_by("placa")
    )


@dataclass
class PendenciaLeitura:
    veiculo: Veiculo
    mes: date
    atrasada: bool  # mês que já passou (leitura esquecida)


def pendencias_de_leitura(hoje):
    """Leituras que faltam no mês atual e nos meses anteriores esquecidos (D21).

    Olha até MESES_COBRADOS_NO_PAINEL meses (contando o atual). Mês anterior à
    primeira leitura do carro (ou à compra) não é cobrado — o sistema ainda não
    o acompanhava. Carro sem nenhuma leitura cobra só o mês atual. Esquecidos
    primeiro, depois por placa. 3 queries, qualquer tamanho de frota.
    """
    atual = primeiro_dia_do_mes(hoje)
    meses = [atual]
    for _ in range(MESES_COBRADOS_NO_PAINEL - 1):
        meses.insert(0, mes_anterior(meses[0]))
    veiculos = list(
        Veiculo.objects.filter(uso=Veiculo.Uso.LOCACAO)
        .exclude(status__in=[Veiculo.Status.VENDIDO, Veiculo.Status.INATIVO])
        .annotate(primeiro_mes_lido=Min("registros_km__mes_referencia"))
        .order_by("placa")
    )
    lidos = set(
        RegistroKm.objects.filter(
            veiculo__in=[v.pk for v in veiculos], mes_referencia__gte=meses[0]
        ).values_list("veiculo_id", "mes_referencia")
    )
    pendencias = []
    for veiculo in veiculos:
        inicio = veiculo.primeiro_mes_lido or atual
        if veiculo.data_aquisicao:
            inicio = max(inicio, primeiro_dia_do_mes(veiculo.data_aquisicao))
        for mes in meses:
            if mes >= inicio and (veiculo.pk, mes) not in lidos:
                pendencias.append(PendenciaLeitura(veiculo, mes, mes < atual))
    pendencias.sort(key=lambda p: (not p.atrasada, p.mes, p.veiculo.placa))
    return pendencias


def mes_padrao_da_lista(hoje):
    """Mês que a tela de KM abre: nos primeiros dias, o anterior se ficou leitura
    dele por fazer (é a leitura de fechamento); senão o mês atual (D21)."""
    atual = primeiro_dia_do_mes(hoje)
    if hoje.day <= DIAS_DE_FECHAMENTO:
        anterior = mes_anterior(atual)
        if any(p.mes == anterior for p in pendencias_de_leitura(hoje)):
            return anterior
    return atual
