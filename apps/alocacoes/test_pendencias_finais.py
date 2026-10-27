"""Etapa 16: carro que ainda está na oficina não volta "Disponível".

Encerrar a alocação ou devolver o substituto com manutenção em aberto
(entrada sem saída) deixa o carro "Em manutenção" — mesma regra da oficina.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.frota.models import Veiculo
from apps.manutencao.models import Manutencao
from apps.manutencao.services import pode_voltar_a_disponivel
from apps.pessoas.models import Cliente

HOJE = timezone.localdate()


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="FIN1A11", marca_modelo="Onix", km_atual=40_000)


@pytest.fixture
def substituto(db):
    return Veiculo.objects.create(placa="FIN2B22", marca_modelo="HB20", km_atual=30_000)


@pytest.fixture
def alocacao(veiculo):
    cliente = Cliente.objects.create(nome="Davi", cpf_cnpj="222.333.444-55")
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=5),
        valor_semanal=Decimal("700.00"),
        km_entrega=40_000,
    )


def _oficina(veiculo, saida=None):
    return Manutencao.objects.create(
        veiculo=veiculo,
        tipo=Manutencao.Tipo.CORRETIVA,
        data=HOJE - timedelta(days=2),
        data_entrada=HOJE - timedelta(days=2),
        data_saida=saida,
    )


def test_encerrar_com_oficina_aberta_fica_em_manutencao_e_volta_ao_fechar(alocacao, veiculo):
    manutencao = _oficina(veiculo)
    alocacao.encerrar(HOJE, km_devolucao=40_300)
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.EM_MANUTENCAO
    # fechando a ida à oficina, a regra da manutenção libera o carro
    manutencao.data_saida = HOJE
    manutencao.save()
    assert pode_voltar_a_disponivel(veiculo)


def test_encerrar_com_oficina_ja_fechada_fica_disponivel(alocacao, veiculo):
    _oficina(veiculo, saida=HOJE - timedelta(days=1))
    alocacao.encerrar(HOJE, km_devolucao=40_300)
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.DISPONIVEL


def test_devolver_troca_com_substituto_na_oficina_fica_em_manutencao(alocacao, substituto):
    troca = TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=HOJE - timedelta(days=3),
        km_retirada=30_000,
    )
    _oficina(substituto)
    troca.devolver(HOJE, km_devolucao=30_100)
    substituto.refresh_from_db()
    assert substituto.status == Veiculo.Status.EM_MANUTENCAO
