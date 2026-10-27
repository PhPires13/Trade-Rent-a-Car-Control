"""Etapa 17 (navegador): confirmação do KM fora do normal, mês inválido e texto de sucesso."""

from datetime import date

import pytest

from apps.frota.models import Veiculo
from apps.km.models import RegistroKm

HTMX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(
        placa="NAV1K17", marca_modelo="Gol", km_compra=10_000, km_atual=10_000
    )


def _post(cliente, veiculo, **extra):
    dados = {"data_leitura": "2024-02-10", "km": "100000", "mes": "2024-02"}
    dados.update(extra)
    return cliente.post(f"/km/registrar/{veiculo.pk}/", dados, **HTMX).content.decode()


def test_aviso_e_caixa_vem_antes_do_botao(usuario_logado, veiculo):
    html = _post(usuario_logado, veiculo)
    assert html.index("Sim, o número está certo") < html.index("Registrar mesmo assim")


def test_registrar_mesmo_assim_sem_marcar_a_caixa_explica_o_que_falta(usuario_logado, veiculo):
    primeira = _post(usuario_logado, veiculo)
    assert "para gravar" not in primeira
    segunda = _post(usuario_logado, veiculo, avisado="100000")
    assert "Marque “Sim, o número está certo” para gravar" in segunda
    assert not veiculo.registros_km.exists()
    _post(usuario_logado, veiculo, avisado="100000", confirmo="100000")
    assert veiculo.registros_km.count() == 1


def test_trocou_o_numero_nao_acusa_falta_de_confirmacao(usuario_logado, veiculo):
    html = _post(usuario_logado, veiculo, km="200000", avisado="100000")
    assert "Confira o número" in html
    assert "para gravar" not in html


def test_texto_do_topo_nao_contradiz_a_confirmacao(usuario_logado, veiculo):
    html = usuario_logado.get("/km/").content.decode()
    assert "precisa ser igual ou maior" not in html


@pytest.mark.parametrize("mes", ["abc", "2024-13", "9999-01"])
def test_mes_invalido_avisa(usuario_logado, veiculo, mes):
    html = usuario_logado.get(f"/km/?mes={mes}", follow=True).content.decode()
    assert "Mês inválido" in html


def test_mes_valido_nao_avisa(usuario_logado, veiculo):
    assert "Mês inválido" not in usuario_logado.get("/km/?mes=2024-02").content.decode()
    assert "Mês inválido" not in usuario_logado.get("/km/").content.decode()


def test_mes_antes_da_compra_nao_lista_o_carro(usuario_logado, veiculo):
    veiculo.data_aquisicao = date(2024, 5, 10)
    veiculo.save(update_fields=["data_aquisicao"])
    html = usuario_logado.get("/km/?mes=2024-02").content.decode()
    assert f'id="pendente-{veiculo.pk}"' not in html
    html = usuario_logado.get("/km/?mes=2024-05").content.decode()
    assert f'id="pendente-{veiculo.pk}"' in html


def test_sucesso_diz_a_data_da_leitura_anterior(usuario_logado, veiculo):
    anterior = RegistroKm(
        veiculo=veiculo, mes_referencia=date(2024, 1, 1), data_leitura=date(2024, 1, 10), km=11_000
    )
    anterior.full_clean()
    anterior.save()
    html = _post(usuario_logado, veiculo, km="12000")
    assert "rodou 1.000 km desde a leitura de 10/01/2024" in html


def test_sucesso_da_primeira_leitura_diz_desde_a_compra(usuario_logado, veiculo):
    veiculo.data_aquisicao = date(2024, 2, 1)
    veiculo.save(update_fields=["data_aquisicao"])
    html = _post(usuario_logado, veiculo, km="11000")
    assert "rodou 1.000 km desde a compra" in html


def test_sem_htmx_tambem_explica_a_caixa_em_branco(usuario_logado, veiculo):
    dados = {"data_leitura": "2024-02-10", "km": "100000", "mes": "2024-02", "avisado": "100000"}
    html = usuario_logado.post(f"/km/registrar/{veiculo.pk}/", dados, follow=True).content.decode()
    assert "Marque “Sim, o número está certo” para gravar" in html
    assert not veiculo.registros_km.exists()
