"""Cálculo do status das preventivas por veículo (docs.md §4.5)."""

from dataclasses import dataclass

from apps.frota.models import Veiculo
from apps.km.models import RegistroKm

from .models import IntervaloPersonalizado, ItemPreventiva, Manutencao

MARGEM_ALERTA_KM = 1_000
#: Um carro de app roda ~300 km/dia: 1.000 km de margem viram só 3 dias de aviso.
#: A previsão em dias (média do próprio carro) antecipa o alerta a tempo de agendar.
MARGEM_ALERTA_DIAS = 14


@dataclass
class StatusPreventiva:
    item: ItemPreventiva
    intervalo_km: int
    ultima: Manutencao | None
    km_proximo: int | None
    faltam_km: int | None
    media_km_dia: float | None = None

    # Impede o Django template de tentar instanciar a classe ao resolver Status.X
    do_not_call_in_templates = True

    SEM_REGISTRO = "sem_registro"
    OK = "ok"
    PROXIMA = "proxima"
    VENCIDA = "vencida"

    @property
    def dias_restantes(self):
        """Previsão em dias no ritmo do próprio carro (última leitura mensal)."""
        if self.faltam_km is None or not self.media_km_dia:
            return None
        if self.faltam_km <= 0:
            return 0
        return round(self.faltam_km / self.media_km_dia)

    @property
    def status(self):
        if self.faltam_km is None:
            return self.SEM_REGISTRO
        if self.faltam_km <= 0:
            return self.VENCIDA
        dias = self.dias_restantes
        if self.faltam_km <= MARGEM_ALERTA_KM or (dias is not None and dias <= MARGEM_ALERTA_DIAS):
            return self.PROXIMA
        return self.OK


def _itens_preventivos():
    return list(ItemPreventiva.objects.filter(ativo=True, intervalo_km_padrao__isnull=False))


def _intervalos_personalizados(ids):
    """{(veiculo_id, item_id): intervalo} numa query, para qualquer nº de veículos."""
    return {
        (veiculo_id, item_id): intervalo
        for veiculo_id, item_id, intervalo in IntervaloPersonalizado.objects.filter(
            veiculo_id__in=ids
        ).values_list("veiculo_id", "item_id", "intervalo_km")
    }


def _ultimas_manutencoes(ids, itens):
    """{(veiculo_id, item_id): Manutencao de maior km} numa query.

    Ordena crescente e vai sobrescrevendo: sobra a de maior km de cada par —
    a mesma que o `order_by("-km").first()` por item devolvia, agora em lote.
    """
    ultimas = {}
    for manutencao in Manutencao.objects.filter(
        veiculo_id__in=ids, item_id__in=[item.pk for item in itens], km__isnull=False
    ).order_by("km", "pk"):
        ultimas[(manutencao.veiculo_id, manutencao.item_id)] = manutencao
    return ultimas


def _medias_km_dia(ids):
    """{veiculo_id: km/dia da leitura mensal mais recente} numa query só."""
    medias = {}
    leituras = RegistroKm.objects.filter(
        veiculo_id__in=ids, dias__isnull=False, km_anterior__isnull=False
    ).order_by("mes_referencia")
    for veiculo_id, km, km_anterior, dias in leituras.values_list(
        "veiculo_id", "km", "km_anterior", "dias"
    ):
        if dias and km >= km_anterior:
            medias[veiculo_id] = (km - km_anterior) / dias  # sobrescreve: fica a mais recente
    return medias


def _status_do_veiculo(veiculo, itens, personalizados, ultimas, medias=None):
    """Regra dos intervalos — única fonte da aritmética das preventivas."""
    media = (medias or {}).get(veiculo.pk)
    resultado = []
    for item in itens:
        intervalo = personalizados.get((veiculo.pk, item.pk)) or item.intervalo_km_padrao
        ultima = ultimas.get((veiculo.pk, item.pk))
        if ultima:
            km_proximo = ultima.km + intervalo
            faltam = km_proximo - veiculo.km_atual
        else:
            km_proximo = None
            faltam = None
        resultado.append(
            StatusPreventiva(
                item=item,
                intervalo_km=intervalo,
                ultima=ultima,
                km_proximo=km_proximo,
                faltam_km=faltam,
                media_km_dia=media,
            )
        )
    return resultado


def resumo_preventivas(veiculo):
    """Status de cada item preventivo do plano para um veículo (tela de um carro)."""
    itens = _itens_preventivos()
    return _status_do_veiculo(
        veiculo,
        itens,
        _intervalos_personalizados([veiculo.pk]),
        _ultimas_manutencoes([veiculo.pk], itens),
        _medias_km_dia([veiculo.pk]),
    )


def resumos_por_veiculo(veiculos):
    """{veiculo_id: [StatusPreventiva]} em 3 queries fixas, seja qual for a frota.

    Versão em lote de resumo_preventivas para o painel e as listagens.
    """
    ids = [v.pk for v in veiculos]
    if not ids:
        return {}
    itens = _itens_preventivos()
    personalizados = _intervalos_personalizados(ids)
    ultimas = _ultimas_manutencoes(ids, itens)
    medias = _medias_km_dia(ids)
    return {
        veiculo.pk: _status_do_veiculo(veiculo, itens, personalizados, ultimas, medias)
        for veiculo in veiculos
    }


def preventivas_em_alerta():
    """(veiculo, [StatusPreventiva vencida/próxima]) para a frota de locação ativa."""
    veiculos = list(
        Veiculo.objects.filter(uso=Veiculo.Uso.LOCACAO).exclude(
            status__in=[Veiculo.Status.VENDIDO, Veiculo.Status.INATIVO]
        )
    )
    resumos = resumos_por_veiculo(veiculos)
    alertas = []
    for veiculo in veiculos:
        criticas = [
            p
            for p in resumos[veiculo.pk]
            if p.status in (StatusPreventiva.VENCIDA, StatusPreventiva.PROXIMA)
        ]
        if criticas:
            alertas.append((veiculo, criticas))
    return alertas


def contagem_alertas_por_veiculo(veiculos):
    """{veiculo_id: nº de preventivas vencidas/próximas} em 3 queries fixas.

    Versão enxuta de resumos_por_veiculo para listagens (o hub da frota);
    segue a mesma regra do painel: só locação, fora vendidos e inativos.
    """
    elegiveis = [
        v
        for v in veiculos
        if v.uso == Veiculo.Uso.LOCACAO
        and v.status not in (Veiculo.Status.VENDIDO, Veiculo.Status.INATIVO)
    ]
    contagens = {}
    for veiculo_id, resumo in resumos_por_veiculo(elegiveis).items():
        alertas = sum(
            1 for p in resumo if p.status in (StatusPreventiva.VENCIDA, StatusPreventiva.PROXIMA)
        )
        if alertas:
            contagens[veiculo_id] = alertas
    return contagens
