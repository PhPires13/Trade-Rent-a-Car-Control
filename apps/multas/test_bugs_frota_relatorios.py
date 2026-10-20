"""D25 (frente frota-relatórios): multa sem motorista na data, de antes da compra e NIC."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.frota.models import Veiculo
from apps.multas import services
from apps.multas.models import Multa
from apps.relatorios import services as relatorios


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(
        placa="ABC1D23", marca_modelo="Onix", data_aquisicao=date(2026, 3, 1)
    )


def test_d25_multa_de_antes_da_compra_vira_vendedor_anterior(veiculo):
    multa = Multa.objects.create(
        veiculo=veiculo, data_infracao=date(2026, 2, 20), valor=Decimal("195.23")
    )
    assert multa.responsavel == Multa.Responsavel.VENDEDOR
    assert multa.repasse == "Não se aplica"


def test_d25_responsavel_escolhido_a_mao_e_respeitado(veiculo):
    multa = Multa.objects.create(
        veiculo=veiculo,
        data_infracao=date(2026, 2, 20),
        valor=Decimal("195.23"),
        responsavel=Multa.Responsavel.EMPRESA,
    )
    assert multa.responsavel == Multa.Responsavel.EMPRESA


def test_d25_multa_sem_cliente_na_data_pede_para_definir_o_responsavel(veiculo):
    multa = Multa.objects.create(  # carro parado no pátio: ninguém estava com ele
        veiculo=veiculo, data_infracao=date(2026, 5, 3), valor=Decimal("195.23")
    )
    assert multa.cliente is None
    assert multa.responsavel == Multa.Responsavel.CLIENTE
    assert multa.repasse == "Sem cliente: definir responsável"


def test_d25_nic_entra_na_data_em_que_foi_registrada(veiculo):
    hoje = date.today()
    original = Multa.objects.create(
        veiculo=veiculo,
        data_infracao=hoje - timedelta(days=70),
        valor=Decimal("195.23"),
        responsavel=Multa.Responsavel.EMPRESA,
    )
    nic = services.registrar_nic(original, valor=Decimal("195.30"))
    assert nic.data_infracao == hoje
    # a despesa da NIC cai no mês do registro, não no mês já fechado da infração
    mes_original = relatorios.despesas_do_mes(
        original.data_infracao.year, original.data_infracao.month
    )
    assert nic not in mes_original["multas_empresa"]
    assert nic in relatorios.despesas_do_mes(hoje.year, hoje.month)["multas_empresa"]


def test_d25_nic_nao_herda_o_cliente_que_esta_com_o_carro_hoje(veiculo):
    """Revisão: com a data de hoje, a NIC buscava o cliente vigente HOJE — outro motorista."""
    from apps.alocacoes.models import Alocacao
    from apps.pessoas.models import Cliente

    hoje = date.today()
    original = Multa.objects.create(  # carro parado no pátio na data da infração
        veiculo=veiculo,
        data_infracao=hoje - timedelta(days=70),
        valor=Decimal("195.23"),
        responsavel=Multa.Responsavel.EMPRESA,
    )
    assert original.cliente is None
    atual = Cliente.objects.create(nome="Bia", cpf_cnpj="999.888.777-66")
    Alocacao.objects.create(
        veiculo=veiculo,
        cliente=atual,
        data_inicio=hoje - timedelta(days=10),
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
    )
    nic = services.registrar_nic(original, valor=Decimal("195.30"))
    nic.refresh_from_db()
    assert nic.cliente is None
