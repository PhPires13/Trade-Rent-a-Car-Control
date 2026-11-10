"""Conferência final da etapa 18b — ND de multas com valor negativo.

Pelo Admin dá para gravar uma multa com valor negativo (o formulário de lá não
tem a conferência da tela). Ela aparece "A cobrar" e, com a trava nova do banco
(item de ND só com valor maior que zero), o "Gerar ND" virava erro 500. Antes
da trava era pior: saía uma ND com total menor que o devido. Agora a ND não
sai e a tela diz o que corrigir.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.financeiro.models import Cobranca, ItemNotaDebito, NotaDebito
from apps.frota.models import Veiculo
from apps.multas import services
from apps.multas.models import Multa
from apps.pessoas.models import Cliente

HOJE = timezone.localdate()


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Diego", cpf_cnpj="111.222.333-44")


@pytest.fixture
def carro(cliente):
    veiculo = Veiculo.objects.create(placa="SHN2E34", marca_modelo="Argo")
    Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=60),
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
    )
    return veiculo


def _multa(veiculo, valor):
    return Multa.objects.create(
        veiculo=veiculo,
        data_infracao=HOJE - timedelta(days=20),
        valor=valor,
        descricao="Avançar sinal",
    )


def test_multa_negativa_nao_vira_nd_nem_erro_500(cliente, carro):
    boa = _multa(carro, Decimal("195.23"))
    torta = _multa(carro, Decimal("-50.00"))
    assert torta.repasse == "A cobrar"  # o caso que chega à tela de gerar ND
    with pytest.raises(ValidationError, match="valor"):
        services.gerar_nd_de_multas(cliente, [boa, torta], HOJE)
    assert not NotaDebito.objects.exists()
    assert not ItemNotaDebito.objects.exists()
    assert not Cobranca.objects.filter(origem=Cobranca.Origem.NOTA_DEBITO).exists()


def test_multa_com_valor_normal_continua_gerando_nd(cliente, carro):
    multa = _multa(carro, Decimal("195.23"))
    nd = services.gerar_nd_de_multas(cliente, [multa], HOJE)
    assert nd.cobranca.valor == Decimal("195.23")
