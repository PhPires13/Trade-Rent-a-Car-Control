"""D01: contrato antigo cadastrado na tela de nova alocação (carga inicial)."""

from datetime import date, timedelta

import pytest

from apps.alocacoes.models import Alocacao
from apps.financeiro.models import Cobranca
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="FLX1A23", marca_modelo="Onix", km_atual=48_900)


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Bruna", cpf_cnpj="222.333.444-55")


def _dados(veiculo, cliente, inicio, **extra):
    return {
        "veiculo": veiculo.pk,
        "cliente": cliente.pk,
        "data_inicio": inicio.isoformat(),
        "valor_semanal": "650,00",
        "dia_vencimento": "",
        "km_entrega": "48900",
        "limite_km": "ilimitado",
        **extra,
    }


def test_contrato_antigo_sem_cobrar_a_partir_de_e_recusado(usuario_logado, veiculo, cliente):
    inicio = date.today() - timedelta(days=60)
    resposta = usuario_logado.post("/alocacoes/nova/", _dados(veiculo, cliente, inicio))
    assert resposta.status_code == 200
    assert "há mais de uma semana" in resposta.content.decode()
    assert not Alocacao.objects.exists()
    assert not Cobranca.objects.exists()


def test_contrato_antigo_so_gera_a_partir_da_data_informada(usuario_logado, veiculo, cliente):
    hoje = date.today()
    inicio = hoje - timedelta(days=63)  # mesmo dia da semana de hoje
    resposta = usuario_logado.post(
        "/alocacoes/nova/",
        _dados(veiculo, cliente, inicio, cobrar_a_partir_de=hoje.isoformat()),
    )
    assert resposta.status_code == 302
    alocacao = Alocacao.objects.get()
    assert alocacao.data_inicio == inicio
    alugueis = list(alocacao.cobrancas.filter(origem=Cobranca.Origem.ALUGUEL))
    assert [c.vencimento for c in alugueis] == [hoje]
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.ATIVO


def test_contrato_antigo_pode_cobrar_desde_o_inicio(usuario_logado, veiculo, cliente):
    inicio = date.today() - timedelta(days=14)
    usuario_logado.post(
        "/alocacoes/nova/",
        _dados(veiculo, cliente, inicio, cobrar_a_partir_de=inicio.isoformat()),
    )
    assert Alocacao.objects.get().cobrancas.filter(origem="aluguel").count() == 3


def test_contrato_novo_nao_pede_a_data(usuario_logado, veiculo, cliente):
    inicio = date.today() - timedelta(days=2)
    resposta = usuario_logado.post("/alocacoes/nova/", _dados(veiculo, cliente, inicio))
    assert resposta.status_code == 302
    assert Alocacao.objects.get().cobrar_a_partir_de is None


def test_tela_mostra_o_campo(usuario_logado, veiculo, cliente):
    html = usuario_logado.get("/alocacoes/nova/").content.decode()
    assert 'name="cobrar_a_partir_de"' in html
    assert "Cobrar aluguel a partir de" in html


def test_cobrar_a_partir_de_longe_demais_e_recusado(usuario_logado, veiculo, cliente):
    # ano digitado errado: o carro rodaria um ano sem cobrança
    inicio = date.today() - timedelta(days=60)
    resposta = usuario_logado.post(
        "/alocacoes/nova/",
        _dados(
            veiculo,
            cliente,
            inicio,
            cobrar_a_partir_de=(date.today() + timedelta(days=365)).isoformat(),
        ),
    )
    assert resposta.status_code == 200
    assert "longe demais" in resposta.content.decode()
    assert not Alocacao.objects.exists()
