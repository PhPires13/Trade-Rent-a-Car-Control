"""Ficha do cliente com número fixo de consultas, e mesmos saldos da lista."""

from datetime import date
from decimal import Decimal

import pytest

from apps.alocacoes.models import Alocacao
from apps.financeiro import services as financeiro
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente


@pytest.fixture
def cliente_com_historico(db):
    """Três contratos encerrados com caução + um em vigor, várias semanas cobradas."""
    cliente = Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")
    for indice in range(3):
        veiculo = Veiculo.objects.create(placa=f"TQ{indice:02d}B{indice:02d}", marca_modelo="Gol")
        alocacao = Alocacao.objects.create(
            veiculo=veiculo,
            cliente=cliente,
            data_inicio=date(2026, 1 + indice, 1),
            valor_semanal=Decimal("650.00"),
            km_entrega=0,
        )
        financeiro.abrir_caucao(alocacao, Decimal("500.00"), date(2026, 1 + indice, 1), "pix")
        alocacao.encerrar(date(2026, 1 + indice, 20), km_devolucao=1_000)
    veiculo = Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol")
    alocacao = Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
    )
    financeiro.abrir_caucao(alocacao, Decimal("1000.00"), date(2026, 7, 1), "pix")
    financeiro.gerar_cobrancas_semanais(hoje=date(2026, 8, 26))  # 9 semanas × 650
    primeira = cliente.cobrancas.order_by("vencimento").first()
    financeiro.registrar_recebimento(
        cliente, date(2026, 7, 2), Decimal("200.00"), "pix", [(primeira, Decimal("200.00"))]
    )
    return cliente


def test_detalhe_nao_cresce_com_cobrancas_e_caucoes(
    usuario_logado, cliente_com_historico, django_assert_max_num_queries
):
    with django_assert_max_num_queries(20):
        resposta = usuario_logado.get(f"/clientes/{cliente_com_historico.pk}/")
    assert resposta.status_code == 200


def test_detalhe_e_lista_mostram_os_mesmos_saldos(usuario_logado, cliente_com_historico):
    """Saldo devedor (9 × 650 − 200) e caução retida (3 × 500 + 1000) iguais nas duas telas."""
    cliente = cliente_com_historico
    detalhe = usuario_logado.get(f"/clientes/{cliente.pk}/").context
    card = usuario_logado.get("/clientes/").context["cards"][0]
    assert detalhe["saldo_devedor"] == card["saldo_devedor"] == Decimal("5650.00")
    assert detalhe["caucao"] == card["caucao"] == Decimal("2500.00")
    assert detalhe["credito"] == card["credito"] == Decimal("0.00")
    assert detalhe["alocacao_ativa"].veiculo.placa == "QXQ6C10"
