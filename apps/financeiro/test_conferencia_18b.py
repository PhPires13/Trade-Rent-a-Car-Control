"""Conferência final da etapa 18b — Receber: caução do contrato numa consulta só.

A tela de Receber passou a mostrar o saldo e o combinado da caução (para pedir
confirmação de sobra acima do combinado). Cada abertura somava a caução em 3
consultas e lia o contrato em mais 1. Agora vem tudo junto.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.financeiro import services
from apps.financeiro.models import MovimentacaoCaucao
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente


@pytest.fixture
def alocacao(db):
    hoje = timezone.localdate()
    alocacao = Alocacao.objects.create(
        veiculo=Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_atual=50_000),
        cliente=Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44"),
        data_inicio=hoje - timedelta(days=30),
        valor_semanal=Decimal("650.00"),
        km_entrega=50_000,
        caucao_valor=Decimal("1500.00"),
    )
    caucao = services.abrir_caucao(alocacao, Decimal("1000.00"), hoje - timedelta(days=30), "pix")
    MovimentacaoCaucao.objects.create(
        caucao=caucao,
        tipo=MovimentacaoCaucao.Tipo.DEVOLUCAO,
        valor=Decimal("100.00"),
        data=hoje - timedelta(days=5),
    )
    services.gerar_cobrancas_semanais(hoje=hoje, alocacao=alocacao)
    return alocacao


def test_receber_le_a_caucao_numa_consulta_so(
    usuario_logado, alocacao, django_assert_max_num_queries
):
    # eram 13 (3 somas da caução + o contrato lidos à parte); 9 antes da etapa 18
    with django_assert_max_num_queries(10):
        resposta = usuario_logado.get(f"/financeiro/receber/?cliente={alocacao.cliente_id}")
    assert resposta.status_code == 200
    assert resposta.context["tem_caucao"] is True
    assert resposta.context["caucao_saldo"] == Decimal("900.00")  # 1.000 depositado - 100
    assert resposta.context["caucao_combinado"] == Decimal("1500.00")


def test_receber_sem_contrato_ativo_nao_tem_caucao(usuario_logado, alocacao):
    alocacao.encerrar(timezone.localdate(), km_devolucao=51_000)
    resposta = usuario_logado.get(f"/financeiro/receber/?cliente={alocacao.cliente_id}")
    assert resposta.status_code == 200
    assert resposta.context["tem_caucao"] is False
    assert resposta.context["caucao_saldo"] == Decimal("0.00")
    assert resposta.context["caucao_combinado"] == Decimal("0.00")
