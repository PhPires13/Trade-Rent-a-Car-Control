"""Etapa 11 — previsão das preventivas em dias (média de km/dia do carro)."""

from datetime import date

import pytest

from apps.frota.models import Veiculo
from apps.km.models import RegistroKm
from apps.manutencao.models import ItemPreventiva, Manutencao
from apps.manutencao.services import StatusPreventiva, resumo_preventivas


@pytest.fixture
def oleo(db):
    item, _ = ItemPreventiva.objects.get_or_create(
        nome="Troca de óleo e filtro", defaults={"intervalo_km_padrao": 10_000}
    )
    ItemPreventiva.objects.exclude(pk=item.pk).update(ativo=False)  # isola o plano no teste
    return item


def leitura(veiculo, data_leitura, km):
    registro = RegistroKm(
        veiculo=veiculo,
        mes_referencia=data_leitura.replace(day=1),
        data_leitura=data_leitura,
        km=km,
    )
    registro.save()
    return registro


def test_previsao_em_dias_usa_a_media_do_proprio_carro(oleo, db):
    veiculo = Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_compra=50_000)
    Manutencao.objects.create(
        veiculo=veiculo, tipo="preventiva", item=oleo, data=date(2026, 6, 1), km=50_000
    )
    leitura(veiculo, date(2026, 6, 30), 53_000)
    leitura(veiculo, date(2026, 7, 30), 56_000)  # 3.000 km em 30 dias → 100 km/dia
    veiculo.refresh_from_db()
    (p,) = resumo_preventivas(veiculo)
    assert p.faltam_km == 4_000  # próximo aos 60.000, odômetro em 56.000
    assert p.dias_restantes == 40  # 4.000 km ÷ 100 km/dia


def test_carro_intenso_entra_em_alerta_pelos_dias(oleo, db):
    """~300 km/dia: o alerta por dias chega com quase um mês de antecedência."""
    veiculo = Veiculo.objects.create(placa="RNB9J66", marca_modelo="Voyage", km_compra=50_000)
    Manutencao.objects.create(
        veiculo=veiculo, tipo="preventiva", item=oleo, data=date(2026, 6, 1), km=50_000
    )
    leitura(veiculo, date(2026, 6, 30), 51_400)
    leitura(veiculo, date(2026, 7, 10), 54_400)  # 3.000 km em 10 dias = 300 km/dia
    veiculo.refresh_from_db()
    (p,) = resumo_preventivas(veiculo)
    assert p.km_proximo == 60_000
    assert p.faltam_km == 5_600  # margem de km (1.000) nem chega perto
    assert p.dias_restantes == 19  # mas no ritmo de 300 km/dia são ~19 dias
    assert p.status == StatusPreventiva.OK  # 19 > 14: ainda ok, alerta vem na próxima leitura


def test_alerta_antecipado_por_dias_sem_estourar_km(oleo, db):
    veiculo = Veiculo.objects.create(placa="TTT1A11", marca_modelo="Gol", km_compra=50_000)
    Manutencao.objects.create(
        veiculo=veiculo, tipo="preventiva", item=oleo, data=date(2026, 6, 1), km=50_000
    )
    leitura(veiculo, date(2026, 6, 30), 53_000)
    leitura(veiculo, date(2026, 7, 31), 57_000)  # 4.000 km em 31 dias ≈ 129 km/dia
    veiculo.refresh_from_db()
    (p,) = resumo_preventivas(veiculo)
    # faltam 3.000 km (bem acima da margem de 1.000), mas ≈ 23 dias — ainda OK…
    assert p.dias_restantes == 23
    assert p.status == StatusPreventiva.OK
    # …roda mais uma semana forte e o alerta chega ANTES da margem de km
    leitura(veiculo, date(2026, 8, 10), 58_400)  # 1.400 km em 10 dias = 140 km/dia
    veiculo.refresh_from_db()
    (p,) = resumo_preventivas(veiculo)
    assert p.faltam_km == 1_600  # ainda acima da margem de 1.000 km
    assert p.dias_restantes == 11  # mas menos de 14 dias no ritmo atual
    assert p.status == StatusPreventiva.PROXIMA


def test_sem_leitura_de_km_nao_ha_previsao(oleo, db):
    veiculo = Veiculo.objects.create(placa="UUU2B22", marca_modelo="Gol", km_atual=59_200)
    Manutencao.objects.create(
        veiculo=veiculo, tipo="preventiva", item=oleo, data=date(2026, 6, 1), km=50_000
    )
    (p,) = resumo_preventivas(veiculo)
    assert p.dias_restantes is None
    assert p.status == StatusPreventiva.PROXIMA  # regra antiga de km continua valendo
