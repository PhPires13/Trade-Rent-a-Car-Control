"""Telas de manutenção com nº de queries fixo, seja qual for o histórico."""

from datetime import date
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.financeiro.models import Cobranca
from apps.frota.models import Veiculo
from apps.manutencao.models import ItemPreventiva, Manutencao
from apps.pessoas.models import Cliente


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_atual=95_500)


def _manutencoes_com_repasse(veiculo, quantidade):
    cliente = Cliente.objects.create(nome="Arlen", cpf_cnpj=f"111.222.333-{quantidade:02d}")
    for indice in range(quantidade):
        cobranca = Cobranca.objects.create(
            cliente=cliente,
            origem=Cobranca.Origem.REPASSE_MANUTENCAO,
            descricao=f"Repasse {indice}",
            valor=Decimal("100.00"),
            vencimento=date(2026, 6, 1),
        )
        Manutencao.objects.create(
            veiculo=veiculo,
            tipo="corretiva",
            data=date(2026, 5, 1 + indice),
            descricao=f"Reparo {indice}",
            responsavel=Manutencao.Responsavel.CLIENTE,
            valor_cobrado_cliente=Decimal("100.00"),
            cobranca_repasse=cobranca,
        )


def _queries_da_tela(cliente_http, url):
    with CaptureQueriesContext(connection) as consultas:
        resposta = cliente_http.get(url)
    assert resposta.status_code == 200
    return len(consultas), resposta.content.decode()


def test_historico_com_repasses_nao_consulta_por_linha(usuario_logado, veiculo):
    _manutencoes_com_repasse(veiculo, 2)
    poucas, _ = _queries_da_tela(usuario_logado, f"/manutencao/historico/{veiculo.pk}/")
    _manutencoes_com_repasse(veiculo, 8)
    muitas, html = _queries_da_tela(usuario_logado, f"/manutencao/historico/{veiculo.pk}/")
    assert html.count(">Cobrado</span>") == 10
    assert muitas == poucas


def test_quadro_de_preventivas_nao_consulta_por_veiculo(usuario_logado):
    oleo = ItemPreventiva.objects.get(nome="Troca de óleo e filtro")

    def frota(inicio, quantidade):
        for indice in range(inicio, inicio + quantidade):
            carro = Veiculo.objects.create(
                placa=f"TQ{indice:02d}A{indice:02d}", marca_modelo="Gol", km_atual=100_000
            )
            Manutencao.objects.create(
                veiculo=carro,
                item=oleo,
                tipo="preventiva",
                data=date(2026, 1, 1),
                km=95_000,
                descricao="Troca de óleo",
            )

    frota(0, 2)
    poucas, _ = _queries_da_tela(usuario_logado, "/manutencao/preventivas/")
    frota(2, 8)
    muitas, html = _queries_da_tela(usuario_logado, "/manutencao/preventivas/")
    assert html.count("+ Registrar manutenção") == 10
    assert muitas == poucas


def test_admin_de_manutencoes_nao_consulta_item_por_linha(admin_client, veiculo):
    oleo = ItemPreventiva.objects.get(nome="Troca de óleo e filtro")

    def manutencoes(quantidade):
        for indice in range(quantidade):
            Manutencao.objects.create(
                veiculo=veiculo,
                item=oleo,
                tipo="preventiva",
                data=date(2026, 1, 1 + indice),
                km=90_000 + indice,
                descricao="Troca de óleo",
            )

    manutencoes(2)
    poucas, _ = _queries_da_tela(admin_client, "/admin/manutencao/manutencao/")
    manutencoes(8)
    muitas, _ = _queries_da_tela(admin_client, "/admin/manutencao/manutencao/")
    assert muitas == poucas
