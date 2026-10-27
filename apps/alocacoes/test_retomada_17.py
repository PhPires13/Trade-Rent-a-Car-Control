"""Etapa 17 (retomada) — vistoria do carro substituto: a data mínima da tela é
a retirada do substituto, não o início da locação."""

import json
from datetime import date

import pytest

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.alocacoes.views import VistoriaForm
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente


@pytest.fixture
def alocacao(db):
    veiculo = Veiculo.objects.create(placa="QXQ7R19", marca_modelo="Gol", km_atual=50_000)
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
    carro = Veiculo.objects.create(placa="RNB7R19", marca_modelo="Voyage", km_atual=40_000)
    TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=carro,
        data_retirada=date(2026, 7, 10),
        km_retirada=40_000,
    )
    return carro


def test_sem_troca_minimo_e_o_inicio_da_locacao(alocacao):
    form = VistoriaForm(alocacao=alocacao)
    assert form.fields["data"].widget.attrs["min"] == "2026-07-01"


def test_com_troca_minimo_e_a_retirada_do_substituto(alocacao, substituto):
    form = VistoriaForm(alocacao=alocacao)
    assert form.fields["data"].widget.attrs["min"] == "2026-07-10"
    inicios = json.loads(form.fields["veiculo"].widget.attrs["data-inicios"])
    assert inicios == {str(alocacao.veiculo_id): "2026-07-01", str(substituto.pk): "2026-07-10"}


def test_carro_da_locacao_escolhido_volta_o_minimo_do_inicio(alocacao, substituto):
    form = VistoriaForm({"veiculo": str(alocacao.veiculo_id)}, alocacao=alocacao)
    assert form.fields["data"].widget.attrs["min"] == "2026-07-01"


def test_tela_troca_o_minimo_ao_mudar_o_carro(usuario_logado, alocacao, substituto):
    html = usuario_logado.get(f"/alocacoes/{alocacao.pk}/vistoria/nova/").content.decode()
    assert 'min="2026-07-10"' in html
    assert "data-inicios" in html
    assert "data.min = inicios[carro.value]" in html
