"""Etapa 17 (retomada) — revisão das sobras da frota: a mensagem do KM atual
aponta o caminho certo, e seguir esse caminho de fato destrava a edição."""

from datetime import date

import pytest

from apps.frota.views import VeiculoForm
from apps.km.correcao import corrigir_leitura
from apps.km.models import RegistroKm
from apps.manutencao.models import Manutencao


@pytest.fixture
def veiculo(db):
    from apps.frota.models import Veiculo

    return Veiculo.objects.create(placa="QXQ7R27", marca_modelo="Gol", km_atual=95_000)


def _dados(veiculo, km):
    return {
        "placa": veiculo.placa,
        "marca_modelo": veiculo.marca_modelo,
        "uso": veiculo.uso,
        "km_atual": km,
        "chave_reserva": veiculo.chave_reserva,
    }


def test_km_da_manutencao_diz_onde_fica_o_editar(veiculo):
    Manutencao.objects.create(veiculo=veiculo, data=date(2026, 10, 5), km=950_000)
    veiculo.refresh_from_db()
    erro = VeiculoForm(_dados(veiculo, 95_000), instance=veiculo).errors["km_atual"][0]
    assert "“Manutenções” na ficha deste carro" in erro and "“Editar”" in erro


def test_corrigir_a_leitura_apontada_destrava_a_edicao(veiculo):
    """A mensagem promete que o KM atual se ajusta sozinho — confere de verdade."""
    leitura = RegistroKm.objects.create(
        veiculo=veiculo, mes_referencia=date(2026, 9, 1), data_leitura=date(2026, 9, 30), km=950_000
    )
    veiculo.refresh_from_db()
    assert veiculo.km_atual == 950_000
    erro = VeiculoForm(_dados(veiculo, 96_000), instance=veiculo).errors["km_atual"][0]
    assert "leitura de KM de 09/2026" in erro

    leitura.km = 96_000
    corrigir_leitura(leitura, 950_000)
    veiculo.refresh_from_db()
    assert veiculo.km_atual == 96_000
    assert VeiculoForm(_dados(veiculo, 96_000), instance=veiculo).is_valid()


def test_corrigir_a_manutencao_apontada_destrava_a_edicao(usuario_logado, veiculo):
    manutencao = Manutencao.objects.create(
        veiculo=veiculo, tipo="corretiva", data=date(2026, 10, 5), km=950_000, descricao="Embreagem"
    )
    veiculo.refresh_from_db()
    assert veiculo.km_atual == 950_000
    resposta = usuario_logado.post(
        f"/manutencao/editar/{manutencao.pk}/",
        {
            "tipo": "corretiva",
            "data": "2026-10-05",
            "km": "96000",
            "descricao": "Embreagem",
            "origem_custo": "particular",
            "responsavel": "empresa",
            "pagamento_custo": "pendente",
            "confirmar_km": "on",
        },
    )
    assert resposta.status_code == 302
    veiculo.refresh_from_db()
    assert veiculo.km_atual == 96_000
    assert VeiculoForm(_dados(veiculo, 96_000), instance=veiculo).is_valid()
