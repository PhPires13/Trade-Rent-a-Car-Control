"""Cálculo do status das preventivas por veículo (docs.md §4.5)."""

from dataclasses import dataclass, field
from datetime import date

from django.utils import timezone

from apps.frota.models import Veiculo
from apps.km.models import RegistroKm

from .models import IntervaloPersonalizado, ItemPreventiva, Manutencao

MARGEM_ALERTA_KM = 1_000
#: Um carro de app roda ~300 km/dia: 1.000 km de margem viram só 3 dias de aviso.
#: A previsão em dias (média do próprio carro) antecipa o alerta a tempo de agendar.
MARGEM_ALERTA_DIAS = 14
#: Carros que saem do acompanhamento das preventivas (painel, quadro e hub da frota).
STATUS_FORA_DO_ACOMPANHAMENTO = (Veiculo.Status.VENDIDO, Veiculo.Status.INATIVO)


def frota_acompanhada():
    """Veículos de locação cujas preventivas são acompanhadas: fora vendidos e inativos."""
    return Veiculo.objects.filter(uso=Veiculo.Uso.LOCACAO).exclude(
        status__in=STATUS_FORA_DO_ACOMPANHAMENTO
    )


def texto_faltam_km(faltam_km):
    """Quanto falta (ou passou) para a troca, em texto simples."""
    from apps.formatos import milhar

    if faltam_km is None:
        return None
    if faltam_km == 0:
        return "vence agora"
    if faltam_km < 0:
        return f"vencida há {milhar(-faltam_km)} km"
    return f"faltam {milhar(faltam_km)} km"


@dataclass
class StatusPreventiva:
    item: ItemPreventiva
    intervalo_km: int
    ultima: Manutencao | None
    km_proximo: int | None
    faltam_km: int | None
    media_km_dia: float | None = None
    #: última leitura mensal (km, data): base da projeção do KM de hoje
    km_leitura: int | None = None
    data_leitura: date | None = None
    hoje: date = field(default_factory=timezone.localdate)

    # Impede o Django template de tentar instanciar a classe ao resolver Status.X
    do_not_call_in_templates = True

    SEM_REGISTRO = "sem_registro"
    OK = "ok"
    PROXIMA = "proxima"
    VENCIDA = "vencida"

    @property
    def sem_registro(self):
        """Nunca houve troca registrada: o 1º ciclo conta do KM da compra (se houver)."""
        return self.ultima is None

    @property
    def km_projetado(self):
        """KM de hoje estimado no ritmo do carro desde a última leitura.

        Entre uma leitura mensal e outra o km_atual fica parado; sem projetar,
        um carro de 300 km/dia vence sem o aviso antecipado aparecer.
        """
        if self.km_proximo is None or self.faltam_km is None:
            return None
        base = self.km_proximo - self.faltam_km  # o KM atual do carro
        if not self.media_km_dia or self.km_leitura is None or self.data_leitura is None:
            return base
        dias = max((self.hoje - self.data_leitura).days, 0)
        return max(base, round(self.km_leitura + self.media_km_dia * dias))

    @property
    def dias_restantes(self):
        """Previsão em dias no ritmo do próprio carro, descontados os dias desde a leitura."""
        if self.faltam_km is None or not self.media_km_dia:
            return None
        if self.faltam_km <= 0:
            return 0
        faltam_hoje = self.km_proximo - self.km_projetado
        if faltam_hoje <= 0:
            return 0
        return round(faltam_hoje / self.media_km_dia)

    @property
    def status(self):
        if self.faltam_km is None:
            return self.SEM_REGISTRO
        if self.faltam_km <= 0:
            return self.VENCIDA
        dias = self.dias_restantes
        if self.faltam_km <= MARGEM_ALERTA_KM or (dias is not None and dias <= MARGEM_ALERTA_DIAS):
            return self.PROXIMA
        if self.sem_registro:
            return self.SEM_REGISTRO  # pelo KM da compra ainda falta, mas não há troca confirmada
        return self.OK

    @property
    def texto_km(self):
        """ "vence agora" / "vencida há 1.200 km" / "faltam 800 km" (None sem registro).

        Texto único para o quadro de preventivas e o painel ("vencida há 0 km" não existe).
        """
        return texto_faltam_km(self.faltam_km)

    @property
    def em_alerta(self):
        """Vencida ou próxima — o que entra no painel e na contagem do hub."""
        return self.status in (self.VENCIDA, self.PROXIMA)


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
    """{veiculo_id: (km/dia, km, data) da leitura mensal mais recente} numa query só."""
    medias = {}
    leituras = RegistroKm.objects.filter(
        veiculo_id__in=ids, dias__isnull=False, km_anterior__isnull=False
    ).order_by("mes_referencia")
    for veiculo_id, km, km_anterior, dias, data_leitura in leituras.values_list(
        "veiculo_id", "km", "km_anterior", "dias", "data_leitura"
    ):
        if dias and km >= km_anterior:
            # sobrescreve: fica a mais recente
            medias[veiculo_id] = ((km - km_anterior) / dias, km, data_leitura)
    return medias


def _status_do_veiculo(veiculo, itens, personalizados, ultimas, medias, hoje=None):
    """Regra dos intervalos — única fonte da aritmética das preventivas.

    Item nunca registrado: o primeiro ciclo conta do KM da compra; sem ele,
    fica "Sem registro" (não há como saber quando vence).
    """
    media, km_leitura, data_leitura = medias.get(veiculo.pk, (None, None, None))
    resultado = []
    for item in itens:
        intervalo = personalizados.get((veiculo.pk, item.pk)) or item.intervalo_km_padrao
        ultima = ultimas.get((veiculo.pk, item.pk))
        base = ultima.km if ultima else veiculo.km_compra
        if base is not None:
            km_proximo = base + intervalo
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
                km_leitura=km_leitura,
                data_leitura=data_leitura,
                hoje=hoje or timezone.localdate(),
            )
        )
    return resultado


def resumos_por_veiculo(veiculos, hoje=None):
    """{veiculo_id: [StatusPreventiva]} em 4 queries fixas, seja qual for a frota."""
    ids = [v.pk for v in veiculos]
    if not ids:
        return {}
    itens = _itens_preventivos()
    personalizados = _intervalos_personalizados(ids)
    ultimas = _ultimas_manutencoes(ids, itens)
    medias = _medias_km_dia(ids)
    return {
        veiculo.pk: _status_do_veiculo(veiculo, itens, personalizados, ultimas, medias, hoje)
        for veiculo in veiculos
    }


def resumo_preventivas(veiculo, hoje=None):
    """Status de cada item preventivo do plano para um veículo só."""
    return resumos_por_veiculo([veiculo], hoje)[veiculo.pk]


def preventivas_em_alerta():
    """(veiculo, [StatusPreventiva vencida/próxima]) para a frota acompanhada."""
    veiculos = list(frota_acompanhada())
    resumos = resumos_por_veiculo(veiculos)
    alertas = []
    for veiculo in veiculos:
        criticas = [p for p in resumos[veiculo.pk] if p.em_alerta]
        if criticas:
            alertas.append((veiculo, criticas))
    return alertas


def preventivas_sem_registro():
    """(veiculo, [StatusPreventiva]) dos itens nunca registrados e fora do alerta.

    Pendência para o painel: "informe quando foi feita a última troca".
    """
    veiculos = list(frota_acompanhada())
    resumos = resumos_por_veiculo(veiculos)
    pendentes = []
    for veiculo in veiculos:
        itens = [p for p in resumos[veiculo.pk] if p.status == StatusPreventiva.SEM_REGISTRO]
        if itens:
            pendentes.append((veiculo, itens))
    return pendentes


def contagem_alertas_por_veiculo(veiculos):
    """{veiculo_id: nº de preventivas vencidas/próximas} em 4 queries fixas.

    Versão enxuta de resumos_por_veiculo para listagens (o hub da frota);
    segue a mesma regra do painel (frota_acompanhada), aplicada em memória.
    """
    elegiveis = [
        v
        for v in veiculos
        if v.uso == Veiculo.Uso.LOCACAO and v.status not in STATUS_FORA_DO_ACOMPANHAMENTO
    ]
    contagens = {}
    for veiculo_id, resumo in resumos_por_veiculo(elegiveis).items():
        alertas = sum(1 for p in resumo if p.em_alerta)
        if alertas:
            contagens[veiculo_id] = alertas
    return contagens


# --- Carro na oficina (selo derivado, sem mexer no status do carro) ---


def manutencoes_em_aberto():
    """Manutenções com entrada na oficina e sem data de saída: o carro ainda está lá."""
    return Manutencao.objects.filter(data_entrada__isnull=False, data_saida__isnull=True)


def oficina_por_veiculo(ids):
    """{veiculo_id: Manutencao em aberto mais antiga} numa query — para selos em listas."""
    em_aberto = {}
    for manutencao in (
        manutencoes_em_aberto()
        .filter(veiculo_id__in=ids)
        .select_related("oficina", "veiculo")
        .order_by("-data_entrada", "-pk")
    ):
        em_aberto[manutencao.veiculo_id] = manutencao  # sobrescreve: fica a mais antiga
    return em_aberto


def pode_voltar_a_disponivel(veiculo, ignorar=None):
    """O carro pode sair de "Em manutenção" para "Disponível"?

    Só quando está marcado em manutenção, não tem alocação ativa, não está
    emprestado como substituto e não tem outra manutenção ainda em aberto.
    """
    from apps.alocacoes.models import Alocacao, TrocaTemporaria

    if veiculo.status != Veiculo.Status.EM_MANUTENCAO:
        return False
    if Alocacao.objects.filter(veiculo=veiculo, status=Alocacao.Status.ATIVA).exists():
        return False
    if TrocaTemporaria.objects.filter(
        veiculo_substituto=veiculo, data_devolucao__isnull=True
    ).exists():
        return False
    outras = manutencoes_em_aberto().filter(veiculo=veiculo)
    if ignorar is not None:
        outras = outras.exclude(pk=ignorar.pk)
    return not outras.exists()
