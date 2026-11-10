"""Conferência final da etapa 18b — encerrar a alocação × pagamento ao mesmo tempo.

A trava nova do encerrar (FOR UPDATE na alocação, sem travar o cliente antes)
deixava o PostgreSQL num impasse com o Receber que cobra o encargo junto: o
Receber trava o cliente e, ao criar o encargo (que aponta para a alocação),
espera a alocação; o encerrar trava a alocação e, ao guardar o crédito das
semanas não usadas (que aponta para o cliente), espera o cliente. Sem a trava
do cliente, um pagamento gravado no meio do encerrar também ficava preso numa
semana cancelada, sem virar crédito. A ordem certa: cliente primeiro (como no
Receber, na caução e no painel), depois a alocação com FOR NO KEY UPDATE (não
barra quem só cria um registro ligado a ela).
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.db.models.query import QuerySet
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente

HOJE = timezone.localdate()


@pytest.fixture
def alocacao(db):
    veiculo = Veiculo.objects.create(placa="QWE1R23", marca_modelo="Gol", km_atual=40_000)
    cliente = Cliente.objects.create(nome="Rita Souza", cpf_cnpj="529.982.247-25")
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=10),
        valor_semanal=Decimal("650.00"),
        km_entrega=40_000,
    )


def _travas_durante(funcao, monkeypatch):
    """(modelo, no_key) de cada select_for_update chamado, na ordem."""
    chamadas = []
    original = QuerySet.select_for_update

    def espiao(self, *args, **kwargs):
        chamadas.append((self.model, kwargs.get("no_key", False)))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", espiao)
    funcao()
    return chamadas


def test_encerrar_trava_o_cliente_antes_da_alocacao(alocacao, monkeypatch):
    travas = _travas_durante(lambda: alocacao.encerrar(HOJE, 40_500), monkeypatch)
    modelos = [modelo for modelo, _ in travas]
    assert Cliente in modelos and Alocacao in modelos
    assert modelos.index(Cliente) < modelos.index(Alocacao)


def test_trava_da_alocacao_nao_barra_registro_ligado_a_ela(alocacao, monkeypatch):
    """FOR NO KEY UPDATE: o encargo criado noutra aba (FK para a alocação) não espera."""
    travas = _travas_durante(lambda: alocacao.encerrar(HOJE, 40_500), monkeypatch)
    assert [no_key for modelo, no_key in travas if modelo is Alocacao] == [True]


def test_encerrar_duas_vezes_continua_barrado(alocacao):
    from django.core.exceptions import ValidationError

    alocacao.encerrar(HOJE, 40_500)
    velha = Alocacao.objects.get(pk=alocacao.pk)
    velha.status = Alocacao.Status.ATIVA  # a 2ª aba ainda acha que está ativa
    with pytest.raises(ValidationError, match="já encerrada"):
        velha.encerrar(HOJE, 40_600)
