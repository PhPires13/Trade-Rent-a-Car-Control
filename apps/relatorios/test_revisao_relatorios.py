"""Revisão dos relatórios fiscais (auditoria grupo B).

Cobre: despesas do mês com multas absorvidas e custos de compra, export cujo
TOTAL fecha com as próprias linhas, ?mes= inválido, recebíveis e série mensal
com queries fixas.
"""

import csv
import io
from datetime import date
from decimal import Decimal

import pytest

from apps.financeiro.models import AplicacaoRecebimento, Cobranca, Recebimento
from apps.frota.models import Veiculo
from apps.manutencao.models import Manutencao
from apps.multas.models import Multa
from apps.pessoas.models import Cliente
from apps.relatorios import services


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol")


# ---------- despesas do mês completas (docs.md §5, decisão nº 21) ----------


def test_despesas_incluem_multa_absorvida_e_custos_de_compra(veiculo):
    Multa.objects.create(
        veiculo=veiculo,
        data_infracao=date(2026, 7, 5),
        valor=Decimal("390.00"),
        responsavel=Multa.Responsavel.EMPRESA,
        descricao="NIC — condutor não indicado",
    )
    Veiculo.objects.create(
        placa="RNB9J66",
        marca_modelo="Voyage",
        data_aquisicao=date(2026, 7, 10),
        custos_entrada=Decimal("800.00"),
    )
    despesas = services.despesas_do_mes(2026, 7)
    assert despesas["total_multas_empresa"] == Decimal("390.00")
    assert despesas["total_custos_compra"] == Decimal("800.00")
    assert despesas["total_geral"] == Decimal("1190.00")


def test_multa_do_cliente_ou_de_outro_mes_fica_fora(veiculo):
    Multa.objects.create(  # responsabilidade do cliente → vira repasse, não despesa
        veiculo=veiculo, data_infracao=date(2026, 7, 5), valor=Decimal("100.00")
    )
    Multa.objects.create(  # da empresa, mas de junho
        veiculo=veiculo,
        data_infracao=date(2026, 6, 5),
        valor=Decimal("200.00"),
        responsavel=Multa.Responsavel.EMPRESA,
    )
    despesas = services.despesas_do_mes(2026, 7)
    assert despesas["total_multas_empresa"] == Decimal("0.00")


def test_multa_absorvida_aparece_na_tela_de_relatorios(usuario_logado, veiculo):
    Multa.objects.create(
        veiculo=veiculo,
        data_infracao=date(2026, 7, 5),
        valor=Decimal("390.00"),
        responsavel=Multa.Responsavel.EMPRESA,
    )
    html = usuario_logado.get("/relatorios/?mes=2026-07").content.decode()
    assert "Multas absorvidas pela empresa" in html


# ---------- export de despesas: TOTAL fecha com as linhas ----------


def test_export_despesas_total_fecha_com_as_linhas(usuario_logado, db):
    veiculo = Veiculo.objects.create(
        placa="QXQ6C10", marca_modelo="Gol", mensalidade_protecao=Decimal("304.00")
    )
    Manutencao.objects.create(
        veiculo=veiculo,
        tipo="corretiva",
        data=date(2026, 7, 10),
        descricao="Freios",
        custo_real=Decimal("400.00"),
    )
    Multa.objects.create(
        veiculo=veiculo,
        data_infracao=date(2026, 7, 5),
        valor=Decimal("390.00"),
        responsavel=Multa.Responsavel.EMPRESA,
    )
    Veiculo.objects.create(
        placa="RNB9J66",
        marca_modelo="Voyage",
        data_aquisicao=date(2026, 7, 3),
        custos_entrada=Decimal("800.00"),
    )
    Veiculo.objects.create(
        placa="SSS1B11",
        marca_modelo="Onix",
        status=Veiculo.Status.VENDIDO,
        data_venda=date(2026, 7, 20),
        valor_venda=Decimal("30000.00"),
        custos_venda=Decimal("500.00"),
    )
    resposta = usuario_logado.get("/relatorios/?mes=2026-07&exportar=despesas&formato=csv")
    linhas = [linha for linha in csv.reader(io.StringIO(resposta.content.decode())) if linha]
    corpo = [linha for linha in linhas[1:] if linha[2] != "TOTAL"]
    total = next(linha for linha in linhas if linha[2] == "TOTAL")
    origens = {linha[2] for linha in corpo}
    assert {"Multa absorvida pela empresa", "Custos de compra", "Custos de venda"} <= origens
    assert sum(Decimal(linha[4]) for linha in corpo) == Decimal(total[4])  # a planilha fecha


# ---------- ?mes= inválido não derruba os relatórios ----------


@pytest.mark.parametrize("mes", ["2026-13", "2026-00", "banana"])
def test_relatorios_mes_invalido_cai_no_mes_atual(usuario_logado, db, mes):
    assert usuario_logado.get(f"/relatorios/?mes={mes}").status_code == 200


# ---------- desempenho: recebíveis e série mensal com queries fixas ----------


def test_recebiveis_saldo_anotado_e_uma_query(db, django_assert_max_num_queries):
    cliente = Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")
    cobrancas = Cobranca.objects.bulk_create(
        [
            Cobranca(
                cliente=cliente,
                origem=Cobranca.Origem.ALUGUEL,
                descricao=f"Aluguel semana {i}",
                valor=Decimal("650.00"),
                vencimento=date(2026, 7, 1 + i),
            )
            for i in range(3)
        ]
    )
    recebimento = Recebimento.objects.create(
        cliente=cliente, data=date(2026, 7, 2), valor=Decimal("100.00")
    )
    AplicacaoRecebimento.objects.create(
        recebimento=recebimento, cobranca=cobrancas[0], valor=Decimal("100.00")
    )
    with django_assert_max_num_queries(2):
        registros = services.recebiveis_em_aberto()
    assert registros[0]["total"] == Decimal("1850.00")  # 550 + 650 + 650


def test_serie_mensal_roda_com_queries_fixas(db, django_assert_max_num_queries):
    with django_assert_max_num_queries(10):
        pontos = services.serie_mensal(2026, 7)
    assert len(pontos) == 6
