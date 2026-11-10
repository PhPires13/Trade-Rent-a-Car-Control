"""Etapa 18 — manutenção de carro vendido: a ida à oficina é de antes da venda."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.frota.models import Veiculo
from apps.manutencao.models import Manutencao

HOJE = timezone.localdate()
VENDA = HOJE - timedelta(days=10)


@pytest.fixture
def vendido(db):
    return Veiculo.objects.create(
        placa="VEN1A18",
        marca_modelo="Onix",
        km_atual=40_000,
        data_aquisicao=HOJE - timedelta(days=300),
        status=Veiculo.Status.VENDIDO,
        data_venda=VENDA,
    )


def _post(cliente, veiculo, **extra):
    dados = {
        "data": (VENDA - timedelta(days=5)).isoformat(),
        "km": "40000",
        "descricao": "Troca da embreagem",
        "origem_custo": "particular",
        "responsavel": "empresa",
        "pagamento_custo": "pendente",
        "custo_real": "300,00",
    }
    dados.update(extra)
    return cliente.post(f"/manutencao/registrar/{veiculo.pk}/", dados)


def test_saida_da_oficina_depois_da_venda_e_recusada(usuario_logado, vendido):
    resposta = _post(
        usuario_logado,
        vendido,
        data_entrada=(VENDA - timedelta(days=5)).isoformat(),
        data_saida=(VENDA + timedelta(days=3)).isoformat(),
    )
    assert resposta.status_code == 200
    erro = resposta.context["form"].errors["data_saida"][0]
    assert f"vendido em {VENDA:%d/%m/%Y}" in erro
    assert not Manutencao.objects.exists()


def test_entrada_na_oficina_depois_da_venda_e_recusada(usuario_logado, vendido):
    resposta = _post(
        usuario_logado,
        vendido,
        data_entrada=(VENDA + timedelta(days=1)).isoformat(),
        data_saida=(VENDA + timedelta(days=2)).isoformat(),
    )
    assert resposta.status_code == 200
    assert "data_entrada" in resposta.context["form"].errors
    assert not Manutencao.objects.exists()


def test_oficina_inteira_antes_da_venda_e_aceita(usuario_logado, vendido):
    resposta = _post(
        usuario_logado,
        vendido,
        data_entrada=(VENDA - timedelta(days=5)).isoformat(),
        data_saida=VENDA.isoformat(),
    )
    assert resposta.status_code == 302
    assert Manutencao.objects.count() == 1


def test_historico_do_vendido_oferece_so_gasto_de_antes_da_venda(usuario_logado, vendido):
    html = usuario_logado.get(f"/manutencao/historico/{vendido.pk}/").content.decode()
    assert "Gasto de antes da venda" in html
    assert "+ Registrar manutenção" not in html


# --- edição da manutenção do carro vendido -------------------------------------


def _editar(cliente, manutencao, **extra):
    dados = {
        "data": manutencao.data.isoformat(),
        "km": "40000",
        "descricao": manutencao.descricao,
        "origem_custo": "particular",
        "responsavel": "empresa",
        "pagamento_custo": "pendente",
        "custo_real": "300,00",
        "data_entrada": manutencao.data_entrada.isoformat(),
        "data_saida": manutencao.data_saida.isoformat(),
    }
    dados.update(extra)
    return cliente.post(f"/manutencao/editar/{manutencao.pk}/", dados)


@pytest.fixture
def gasto_antigo(vendido):
    antes = VENDA - timedelta(days=5)
    return Manutencao.objects.create(
        veiculo=vendido,
        data=antes,
        km=40_000,
        descricao="Troca da embreagem",
        data_entrada=antes,
        data_saida=VENDA,
        custo_real=300,
    )


def test_edicao_nao_reabre_a_oficina_do_vendido(usuario_logado, gasto_antigo):
    resposta = _editar(usuario_logado, gasto_antigo, data_saida="")
    assert resposta.status_code == 200
    assert "data_saida" in resposta.context["form"].errors
    gasto_antigo.refresh_from_db()
    assert gasto_antigo.data_saida == VENDA


def test_edicao_nao_poe_a_saida_depois_da_venda(usuario_logado, gasto_antigo):
    resposta = _editar(
        usuario_logado, gasto_antigo, data_saida=(VENDA + timedelta(days=2)).isoformat()
    )
    assert resposta.status_code == 200
    assert f"vendido em {VENDA:%d/%m/%Y}" in resposta.context["form"].errors["data_saida"][0]


def test_edicao_do_custo_do_vendido_continua_livre(usuario_logado, gasto_antigo):
    resposta = _editar(usuario_logado, gasto_antigo, custo_real="450,00")
    assert resposta.status_code == 302
    gasto_antigo.refresh_from_db()
    assert gasto_antigo.custo_real == 450


def test_edicao_nao_acusa_o_que_ja_estava_gravado(usuario_logado, vendido):
    """Venda lançada com data de antes de um gasto já gravado: acertar o custo não trava."""
    depois = VENDA + timedelta(days=2)
    manutencao = Manutencao.objects.create(
        veiculo=vendido,
        data=depois,
        km=40_000,
        descricao="Pneu",
        data_entrada=depois,
        data_saida=depois,
        custo_real=200,
    )
    resposta = _editar(usuario_logado, manutencao, custo_real="250,00")
    assert resposta.status_code == 302
