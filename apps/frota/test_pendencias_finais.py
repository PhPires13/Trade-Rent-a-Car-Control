"""Etapa 16: repasse quitado com desconto da caução entra na receita do carro."""

from datetime import date
from decimal import Decimal

import pytest

from apps.alocacoes.models import Alocacao
from apps.financeiro import services as financeiro
from apps.financeiro.models import Cobranca
from apps.frota import desmobilizacao
from apps.frota.models import Veiculo
from apps.manutencao.models import Manutencao
from apps.pessoas.models import Cliente
from apps.sinistros.models import Sinistro

HOJE = date(2026, 10, 26)


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(
        placa="FIN3C33",
        marca_modelo="Gol",
        data_aquisicao=date(2025, 1, 1),
        valor_compra=Decimal("40000.00"),
    )


@pytest.fixture
def caucao(veiculo):
    cliente = Cliente.objects.create(nome="Elisa", cpf_cnpj="555.666.777-88")
    alocacao = Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 9, 1),
        valor_semanal=Decimal("500.00"),
        km_entrega=10_000,
    )
    return financeiro.abrir_caucao(alocacao, Decimal("2000.00"), date(2026, 9, 1), "pix")


def _cobranca(cliente, origem, valor):
    return Cobranca.objects.create(
        cliente=cliente,
        origem=origem,
        descricao="repasse",
        valor=valor,
        vencimento=date(2026, 9, 25),
    )


def test_repasse_de_manutencao_quitado_com_caucao_e_receita_do_carro(veiculo, caucao):
    cliente = caucao.alocacao.cliente
    cobranca = _cobranca(cliente, Cobranca.Origem.REPASSE_MANUTENCAO, Decimal("300.00"))
    Manutencao.objects.create(
        veiculo=veiculo,
        tipo=Manutencao.Tipo.CORRETIVA,
        data=date(2026, 9, 20),
        descricao="Para-choque",
        custo_real=Decimal("300.00"),
        responsavel=Manutencao.Responsavel.CLIENTE,
        valor_cobrado_cliente=Decimal("300.00"),
        cobranca_repasse=cobranca,
    )
    financeiro.descontar_da_caucao(caucao, cobranca, Decimal("300.00"), date(2026, 9, 26))

    ficha = desmobilizacao.montar_ficha(veiculo, HOJE)
    assert ficha.receita_repasses == Decimal("300.00")
    assert ficha.receita_aluguel == Decimal("0")


def test_franquia_quitada_com_caucao_e_receita_do_carro(veiculo, caucao):
    cliente = caucao.alocacao.cliente
    cobranca = _cobranca(cliente, Cobranca.Origem.REPASSE_SINISTRO, Decimal("1500.00"))
    Sinistro.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data=date(2026, 9, 15),
        tipo="colisao",
        franquia_valor=Decimal("1500.00"),
        cobranca_franquia=cobranca,
    )
    financeiro.descontar_da_caucao(caucao, cobranca, Decimal("1500.00"), date(2026, 9, 26))

    ficha = desmobilizacao.montar_ficha(veiculo, HOJE)
    assert ficha.receita_repasses == Decimal("1500.00")
    assert ficha.receita_aluguel == Decimal("0")
    assert ficha.despesa_franquias == Decimal("1500.00")


def test_desconto_de_aluguel_continua_no_aluguel(veiculo, caucao):
    cliente = caucao.alocacao.cliente
    cobranca = Cobranca.objects.create(
        cliente=cliente,
        alocacao=caucao.alocacao,
        origem=Cobranca.Origem.ALUGUEL,
        descricao="Semana",
        valor=Decimal("500.00"),
        vencimento=date(2026, 9, 8),
    )
    financeiro.descontar_da_caucao(caucao, cobranca, Decimal("500.00"), date(2026, 9, 10))

    ficha = desmobilizacao.montar_ficha(veiculo, HOJE)
    assert ficha.receita_aluguel == Decimal("500.00")
    assert ficha.receita_repasses == Decimal("0")


def test_ficha_nao_ganha_consulta_por_causa_do_desconto(
    veiculo, caucao, django_assert_max_num_queries
):
    """Mesmo teto do lote (12): os descontos de aluguel e de repasse saem numa consulta só."""
    with django_assert_max_num_queries(12):
        desmobilizacao.montar_ficha(veiculo, HOJE)
