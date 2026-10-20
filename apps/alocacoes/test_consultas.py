"""Telas de encerramento e troca com número fixo de consultas."""

from datetime import date
from decimal import Decimal

import pytest

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.financeiro import services as financeiro
from apps.financeiro.models import Cobranca
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente


@pytest.fixture
def alocacao(db):
    veiculo = Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_atual=50_000)
    cliente = Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")
    alocacao = Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=50_000,
    )
    financeiro.abrir_caucao(alocacao, Decimal("1000.00"), date(2026, 7, 1), "pix")
    return alocacao


def test_tela_de_encerrar_consulta_a_caucao_uma_vez(
    usuario_logado, alocacao, django_assert_max_num_queries
):
    with django_assert_max_num_queries(8):
        resposta = usuario_logado.get(f"/alocacoes/{alocacao.pk}/encerrar/")
    assert resposta.status_code == 200
    assert "1.000,00" in resposta.content.decode()


def test_encerrar_cancela_semanas_sem_consultar_cobranca_por_cobranca(
    alocacao, django_assert_max_num_queries
):
    financeiro.gerar_cobrancas_semanais(hoje=date(2026, 9, 30))  # 14 semanas
    # Cada semana cancelada custa 2 escritas (save + histórico); a quitação de
    # todas vem numa consulta só — eram mais 2 leituras por semana (54 no total).
    # +3 fixas: encargos das semanas canceladas (1) e inadimplência recalculada (2).
    with django_assert_max_num_queries(38):
        alocacao.encerrar(date(2026, 7, 20), km_devolucao=51_000)
    # Vencidas antes do término ficam; as 11 a partir de 22/07 (sem pagamento) caem.
    canceladas = alocacao.cobrancas.filter(status=Cobranca.Status.CANCELADA)
    assert canceladas.count() == 11
    assert not canceladas.filter(vencimento__lt=date(2026, 7, 20)).exists()


def test_encerrar_mantem_semana_futura_ja_paga(alocacao):
    financeiro.gerar_cobrancas_semanais(hoje=date(2026, 7, 29))
    paga = alocacao.cobrancas.get(vencimento=date(2026, 7, 29))
    financeiro.registrar_recebimento(
        alocacao.cliente, date(2026, 7, 2), Decimal("650.00"), "pix", [(paga, Decimal("650.00"))]
    )
    alocacao.encerrar(date(2026, 7, 20), km_devolucao=51_000)
    paga.refresh_from_db()
    assert paga.status == Cobranca.Status.PAGO


def test_tela_de_devolver_substituto_com_consultas_fixas(
    usuario_logado, alocacao, django_assert_max_num_queries
):
    substituto = Veiculo.objects.create(placa="RUJ3I28", marca_modelo="Onix", km_atual=40_000)
    troca = TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=date(2026, 7, 10),
        km_retirada=40_000,
    )
    with django_assert_max_num_queries(5):
        resposta = usuario_logado.get(f"/alocacoes/troca/{troca.pk}/devolver/")
    assert resposta.status_code == 200
    conteudo = resposta.content.decode()
    assert "Arlen" in conteudo and "RUJ3I28" in conteudo and "QXQ6C10" in conteudo
