"""Conferência final da etapa 18b — Relatórios: caixinha "Ir para" numa consulta só.

primeiro_mes_com_dado() olha 12 fontes (compra, venda, aluguel, pagamento,
cobrança, caução, oficina, multa, sinistro, auxílio, IPVA). Eram 12 idas ao
banco a cada abertura de Relatórios (41 → 54 consultas na tela); agora é uma.
"""

from datetime import date
from decimal import Decimal

import pytest

from apps.frota.models import PagamentoIpva, Veiculo
from apps.manutencao.models import Manutencao
from apps.pessoas.models import Cliente
from apps.relatorios import services
from apps.sinistros.models import AuxilioMotorista, Sinistro


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ7R28", marca_modelo="Gol")


def test_sem_nenhum_dado_e_none(db, django_assert_num_queries):
    with django_assert_num_queries(1):
        assert services.primeiro_mes_com_dado() is None


def test_o_menor_de_todas_as_fontes_numa_consulta(veiculo, django_assert_num_queries):
    veiculo.data_aquisicao = date(2026, 5, 1)
    veiculo.save()
    Manutencao.objects.create(
        veiculo=veiculo,
        tipo="corretiva",
        data=date(2026, 3, 10),
        descricao="Farol",
        custo_real=Decimal("300.00"),
    )
    sinistro = Sinistro.objects.create(
        veiculo=veiculo,
        cliente=Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44"),
        data=date(2026, 4, 1),
        envolvido="terceiro",
    )
    AuxilioMotorista.objects.create(
        sinistro=sinistro, valor=Decimal("500.00"), data_recebimento=date(2026, 2, 20)
    )
    with django_assert_num_queries(1):
        assert services.primeiro_mes_com_dado() == date(2026, 2, 20)


def test_carro_pessoal_continua_fora(veiculo):
    pessoal = Veiculo.objects.create(
        placa="ABC1D23",
        marca_modelo="Corolla",
        uso=Veiculo.Uso.FORA_LOCACAO,
        data_aquisicao=date(2015, 3, 1),
    )
    PagamentoIpva.objects.create(
        veiculo=pessoal, ano=2016, parcela=1, valor=Decimal("900.00"), pago_em=date(2016, 1, 10)
    )
    PagamentoIpva.objects.create(
        veiculo=veiculo, ano=2026, parcela=1, valor=Decimal("900.00"), pago_em=date(2026, 1, 15)
    )
    assert services.primeiro_mes_com_dado() == date(2026, 1, 15)


def test_tela_de_relatorios_sem_as_12_consultas(
    usuario_logado, veiculo, django_assert_max_num_queries
):
    with django_assert_max_num_queries(43):
        resposta = usuario_logado.get("/relatorios/")
    assert resposta.status_code == 200
