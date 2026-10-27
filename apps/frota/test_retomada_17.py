"""Etapa 17 (retomada) — sobras da frota: FICI só das multas pendentes reais e
mensagem do KM atual apontando o lançamento de onde o número veio."""

from datetime import date

import pytest

from apps.frota.alertas import pendencias_do_veiculo
from apps.frota.models import Veiculo
from apps.frota.views import VeiculoForm
from apps.km.models import RegistroKm
from apps.manutencao.models import Manutencao
from apps.multas.models import Multa


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ7R17", marca_modelo="Gol", km_atual=95_000)


def _dados(veiculo, **extra):
    return {
        "placa": veiculo.placa,
        "marca_modelo": veiculo.marca_modelo,
        "uso": veiculo.uso,
        "km_atual": veiculo.km_atual,
        "chave_reserva": veiculo.chave_reserva,
        **extra,
    }


# --- (1) FICI: hub e pendências usam o mesmo critério (Multa.objects.fici_pendente) ---


@pytest.mark.parametrize("resultado", Multa.RESULTADOS_ANULADORES)
def test_hub_e_pendencias_ignoram_fici_de_multa_anulada(usuario_logado, veiculo, resultado):
    Multa.objects.create(veiculo=veiculo, data_infracao=date(2026, 10, 1), resultado=resultado)
    resposta = usuario_logado.get("/frota/")
    card = next(c for c in resposta.context["cards"] if c["veiculo"].pk == veiculo.pk)
    assert card["multas_fici_pendentes"] == 0
    assert not [p for p in pendencias_do_veiculo(veiculo) if "FICI" in p["texto"]]


def test_hub_e_pendencias_contam_fici_de_multa_em_aberto(usuario_logado, veiculo):
    Multa.objects.create(veiculo=veiculo, data_infracao=date(2026, 10, 1))
    Multa.objects.create(
        veiculo=veiculo, data_infracao=date(2026, 10, 2), resultado=Multa.Resultado.CANCELADA
    )
    resposta = usuario_logado.get("/frota/")
    card = next(c for c in resposta.context["cards"] if c["veiculo"].pk == veiculo.pk)
    assert card["multas_fici_pendentes"] == 1
    textos = [p["texto"] for p in pendencias_do_veiculo(veiculo) if "FICI" in p["texto"]]
    assert textos == ["1 multa sem indicar o motorista (FICI)"]


# --- (4) KM atual: continua sem diminuir, mas diz onde corrigir --------------------


def test_km_menor_continua_recusado(veiculo):
    form = VeiculoForm(_dados(veiculo, km_atual=9_500), instance=veiculo)
    assert not form.is_valid()
    assert "não diminui" in form.errors["km_atual"][0]


def test_km_vindo_da_manutencao_manda_corrigir_na_manutencao(veiculo):
    Manutencao.objects.create(veiculo=veiculo, data=date(2026, 10, 5), km=950_000)
    veiculo.refresh_from_db()
    assert veiculo.km_atual == 950_000
    form = VeiculoForm(_dados(veiculo, km_atual=95_000), instance=veiculo)
    assert not form.is_valid()
    erro = form.errors["km_atual"][0]
    assert "manutenção de 05/10/2026" in erro
    assert "Registros de KM" not in erro


def test_km_vindo_da_leitura_manda_corrigir_a_leitura(veiculo):
    RegistroKm.objects.create(
        veiculo=veiculo, mes_referencia=date(2026, 9, 1), data_leitura=date(2026, 9, 30), km=150_000
    )
    veiculo.refresh_from_db()
    assert veiculo.km_atual == 150_000
    form = VeiculoForm(_dados(veiculo, km_atual=95_000), instance=veiculo)
    assert not form.is_valid()
    erro = form.errors["km_atual"][0]
    assert "leitura de KM de 09/2026" in erro
    assert "Corrigir" in erro


def test_km_sem_leitura_nem_manutencao_mantem_a_orientacao_geral(veiculo):
    form = VeiculoForm(_dados(veiculo, km_atual=9_500), instance=veiculo)
    assert not form.is_valid()
    erro = form.errors["km_atual"][0]
    assert "Registros de KM" in erro and "Corrigir" in erro
    assert "Admin" not in erro
