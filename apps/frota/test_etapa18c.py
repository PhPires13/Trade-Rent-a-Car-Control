"""Etapa 18c — frota: sobras da etapa 18 e refatoração com o comportamento travado.

Ficha do carro separa o aluguel do excedente/encargo, vendidos em cartões no
celular, atalho "Dar carro substituto", números formatados na retaguarda e
alvo de toque do <summary> do ranking.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.financeiro import services as financeiro
from apps.financeiro.models import Cobranca
from apps.frota import desmobilizacao
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente

HOJE = timezone.localdate()


@pytest.fixture
def carro(db):
    return Veiculo.objects.create(
        placa="ETA8C18",
        marca_modelo="Onix",
        km_compra=40_000,
        km_atual=40_000,
        data_aquisicao=HOJE - timedelta(days=120),
        valor_compra=Decimal("50000.00"),
    )


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


@pytest.fixture
def alocacao(carro, cliente):
    return Alocacao.objects.create(
        veiculo=carro,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=30),
        valor_semanal=Decimal("650.00"),
        km_entrega=40_000,
    )


def _cobranca(alocacao, origem, valor):
    return Cobranca.objects.create(
        cliente=alocacao.cliente,
        alocacao=alocacao,
        origem=origem,
        descricao=f"Teste {origem}",
        valor=Decimal(valor),
        vencimento=HOJE - timedelta(days=3),
    )


def _pagar(cobranca):
    financeiro.registrar_recebimento(
        cobranca.cliente, HOJE, cobranca.valor, "pix", [(cobranca, cobranca.valor)]
    )


# --- (1) Ficha: "Aluguéis recebidos" só com o aluguel semanal ---------------------------


def test_ficha_separa_aluguel_de_excedente_e_encargo_sem_mudar_o_total(alocacao, carro):
    _pagar(_cobranca(alocacao, Cobranca.Origem.ALUGUEL, "650.00"))
    _pagar(_cobranca(alocacao, Cobranca.Origem.EXCEDENTE_KM, "120.00"))
    _pagar(_cobranca(alocacao, Cobranca.Origem.ENCARGO, "65.00"))
    ficha = desmobilizacao.montar_ficha(carro)
    assert ficha.receita_aluguel == Decimal("650.00")
    assert ficha.receita_extras_locacao == Decimal("185.00")
    # o total da receita continua o mesmo de antes (tudo do contrato entra)
    assert ficha.receita_total == Decimal("835.00")


def test_ficha_separa_desconto_de_caucao_pela_origem(alocacao, carro):
    caucao = financeiro.abrir_caucao(
        alocacao, valor_recebido=Decimal("1000.00"), data=HOJE - timedelta(days=30)
    )
    aluguel = _cobranca(alocacao, Cobranca.Origem.ALUGUEL, "650.00")
    excedente = _cobranca(alocacao, Cobranca.Origem.EXCEDENTE_KM, "100.00")
    financeiro.descontar_da_caucao(caucao, aluguel, Decimal("650.00"), HOJE)
    financeiro.descontar_da_caucao(caucao, excedente, Decimal("100.00"), HOJE)
    ficha = desmobilizacao.montar_ficha(carro)
    assert ficha.receita_aluguel == Decimal("650.00")
    assert ficha.receita_extras_locacao == Decimal("100.00")
    assert ficha.receita_total == Decimal("750.00")


def test_tela_da_ficha_mostra_a_linha_do_excedente_so_quando_ha(usuario_logado, alocacao, carro):
    _pagar(_cobranca(alocacao, Cobranca.Origem.ALUGUEL, "650.00"))
    url = reverse("frota:ficha", args=[carro.pk])
    resposta = usuario_logado.get(url)
    assert "Excedente de km, encargos" not in resposta.content.decode()
    _pagar(_cobranca(alocacao, Cobranca.Origem.ENCARGO, "65.00"))
    html = usuario_logado.get(url).content.decode()
    assert "Excedente de km, encargos e outras cobranças do contrato" in html
    assert "+ R$ 65,00" in html
    assert "+ R$ 650,00" in html
