"""Cálculo do status das preventivas por veículo (docs.md §4.5)."""

from dataclasses import dataclass

from apps.frota.models import Veiculo

from .models import IntervaloPersonalizado, ItemPreventiva, Manutencao

MARGEM_ALERTA_KM = 1_000


@dataclass
class StatusPreventiva:
    item: ItemPreventiva
    intervalo_km: int
    ultima: Manutencao | None
    km_proximo: int | None
    faltam_km: int | None

    # Impede o Django template de tentar instanciar a classe ao resolver Status.X
    do_not_call_in_templates = True

    SEM_REGISTRO = "sem_registro"
    OK = "ok"
    PROXIMA = "proxima"
    VENCIDA = "vencida"

    @property
    def status(self):
        if self.faltam_km is None:
            return self.SEM_REGISTRO
        if self.faltam_km <= 0:
            return self.VENCIDA
        if self.faltam_km <= MARGEM_ALERTA_KM:
            return self.PROXIMA
        return self.OK


def intervalo_do_item(veiculo, item, personalizados=None):
    if personalizados is not None:
        personalizado = personalizados.get(item.pk)
    else:
        registro = IntervaloPersonalizado.objects.filter(veiculo=veiculo, item=item).first()
        personalizado = registro.intervalo_km if registro else None
    return personalizado or item.intervalo_km_padrao


def resumo_preventivas(veiculo):
    """Status de cada item preventivo do plano para um veículo."""
    itens = ItemPreventiva.objects.filter(ativo=True, intervalo_km_padrao__isnull=False)
    personalizados = dict(
        IntervaloPersonalizado.objects.filter(veiculo=veiculo).values_list(
            "item_id", "intervalo_km"
        )
    )
    resultado = []
    for item in itens:
        intervalo = personalizados.get(item.pk) or item.intervalo_km_padrao
        ultima = (
            Manutencao.objects.filter(veiculo=veiculo, item=item, km__isnull=False)
            .order_by("-km")
            .first()
        )
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
            )
        )
    return resultado


def preventivas_em_alerta():
    """(veiculo, [StatusPreventiva vencida/próxima]) para a frota de locação ativa."""
    veiculos = Veiculo.objects.filter(uso=Veiculo.Uso.LOCACAO).exclude(
        status__in=[Veiculo.Status.VENDIDO, Veiculo.Status.INATIVO]
    )
    alertas = []
    for veiculo in veiculos:
        criticas = [
            p
            for p in resumo_preventivas(veiculo)
            if p.status in (StatusPreventiva.VENCIDA, StatusPreventiva.PROXIMA)
        ]
        if criticas:
            alertas.append((veiculo, criticas))
    return alertas


def contagem_alertas_por_veiculo(veiculos):
    """{veiculo_id: nº de preventivas vencidas/próximas} em 3 queries fixas.

    Versão em lote de resumo_preventivas para listagens (o hub da frota);
    segue a mesma regra do painel: só locação, fora vendidos e inativos.
    """
    from django.db.models import Max

    elegiveis = [
        v
        for v in veiculos
        if v.uso == Veiculo.Uso.LOCACAO
        and v.status not in (Veiculo.Status.VENDIDO, Veiculo.Status.INATIVO)
    ]
    if not elegiveis:
        return {}
    ids = [v.pk for v in elegiveis]
    itens = list(ItemPreventiva.objects.filter(ativo=True, intervalo_km_padrao__isnull=False))
    personalizados = {
        (veiculo_id, item_id): intervalo
        for veiculo_id, item_id, intervalo in IntervaloPersonalizado.objects.filter(
            veiculo_id__in=ids
        ).values_list("veiculo_id", "item_id", "intervalo_km")
    }
    ultimos_km = {
        (linha["veiculo_id"], linha["item_id"]): linha["ultimo_km"]
        for linha in Manutencao.objects.filter(
            veiculo_id__in=ids, item__isnull=False, km__isnull=False
        )
        .values("veiculo_id", "item_id")
        .annotate(ultimo_km=Max("km"))
    }
    contagens = {}
    for veiculo in elegiveis:
        alertas = 0
        for item in itens:
            ultimo = ultimos_km.get((veiculo.pk, item.pk))
            if ultimo is None:
                continue
            intervalo = personalizados.get((veiculo.pk, item.pk)) or item.intervalo_km_padrao
            faltam = ultimo + intervalo - veiculo.km_atual
            if faltam <= MARGEM_ALERTA_KM:
                alertas += 1
        if alertas:
            contagens[veiculo.pk] = alertas
    return contagens
