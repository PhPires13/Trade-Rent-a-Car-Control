"""Etapa 17 (retomada) — revisão da vistoria do substituto: o servidor recusa
data antes da retirada (mesmo que o navegador deixe passar) e a tela devolvida
com erro mantém a data mínima do carro escolhido."""

from datetime import date

import pytest

from apps.alocacoes.models import Alocacao, TrocaTemporaria, Vistoria
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente


@pytest.fixture
def alocacao(db):
    veiculo = Veiculo.objects.create(placa="QXQ7R29", marca_modelo="Gol", km_atual=50_000)
    cliente = Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=650,
        km_entrega=50_000,
    )


@pytest.fixture
def substituto(alocacao):
    carro = Veiculo.objects.create(placa="RNB7R29", marca_modelo="Voyage", km_atual=40_000)
    TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=carro,
        data_retirada=date(2026, 7, 10),
        km_retirada=40_000,
    )
    return carro


def _url(alocacao):
    return f"/alocacoes/{alocacao.pk}/vistoria/nova/"


def test_substituto_antes_da_retirada_e_recusado(usuario_logado, alocacao, substituto):
    resposta = usuario_logado.post(
        _url(alocacao),
        {"veiculo": substituto.pk, "tipo": "entrada", "data": "2026-07-05", "km": "40000"},
    )
    assert resposta.status_code == 200
    html = resposta.content.decode()
    assert "antes da retirada do substituto (10/07/2026)" in html
    assert 'min="2026-07-10"' in html  # a tela com erro segue com o mínimo do substituto
    assert not Vistoria.objects.exists()


def test_substituto_no_dia_da_retirada_e_aceito(usuario_logado, alocacao, substituto):
    resposta = usuario_logado.post(
        _url(alocacao),
        {"veiculo": substituto.pk, "tipo": "entrada", "data": "2026-07-10", "km": "40000"},
    )
    assert resposta.status_code == 302
    assert Vistoria.objects.get().veiculo == substituto


def test_carro_da_locacao_durante_a_troca_aceita_desde_o_inicio(
    usuario_logado, alocacao, substituto
):
    resposta = usuario_logado.post(
        _url(alocacao),
        {"veiculo": alocacao.veiculo_id, "tipo": "entrada", "data": "2026-07-05", "km": "50000"},
    )
    assert resposta.status_code == 302
    assert Vistoria.objects.get().veiculo_id == alocacao.veiculo_id
