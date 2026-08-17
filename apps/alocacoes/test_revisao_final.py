"""Ajustes finais da auditoria: coerência de datas, atalhos e placa normalizada."""

from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.frota.models import Veiculo, normalizar_placa
from apps.pessoas.models import Cliente


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_atual=50_000)


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


@pytest.fixture
def alocacao(veiculo, cliente):
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=50_000,
    )


def test_encerrar_com_data_anterior_ao_inicio_e_bloqueado(alocacao):
    with pytest.raises(ValidationError, match="anterior ao início"):
        alocacao.encerrar(date(2026, 6, 20), 51_000)


def test_devolver_troca_antes_da_retirada_e_bloqueado(alocacao, db):
    substituto = Veiculo.objects.create(placa="RNB9J66", marca_modelo="Voyage", km_atual=20_000)
    troca = TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=date(2026, 7, 10),
        km_retirada=20_000,
    )
    with pytest.raises(ValidationError, match="anterior à retirada"):
        troca.devolver(date(2026, 7, 5), 20_500)


def test_alocar_pela_ficha_do_veiculo_ja_vem_com_o_carro(usuario_logado, veiculo):
    html = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/").content.decode()
    assert f"/alocacoes/nova/?veiculo={veiculo.pk}" in html


@pytest.mark.parametrize("digitado", ["qxq-6c10", "QXQ 6C10", "qxq6c10 ", "QXQ-6C10"])
def test_busca_global_acha_placa_digitada_de_qualquer_jeito(usuario_logado, veiculo, digitado):
    resposta = usuario_logado.get("/buscar/", {"q": digitado})
    # resultado único redireciona direto para a ficha do carro
    assert resposta.status_code == 302
    assert resposta.url == f"/frota/veiculo/{veiculo.pk}/"


def test_normalizar_placa_e_a_fonte_unica(db):
    assert normalizar_placa(" abc-1d23 ") == "ABC1D23"
    assert normalizar_placa(None) == ""
    veiculo = Veiculo.objects.create(placa="rnb-9j66", marca_modelo="Voyage")
    veiculo.refresh_from_db()
    assert veiculo.placa == "RNB9J66"
