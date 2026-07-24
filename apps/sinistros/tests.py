from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.alocacoes.models import Alocacao
from apps.frota.models import Veiculo
from apps.manutencao.models import Manutencao
from apps.manutencao.repasse import gerar_repasse
from apps.pessoas.models import Cliente
from apps.sinistros.models import AuxilioMotorista, Sinistro


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol")


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


@pytest.fixture
def alocacao(veiculo, cliente):
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
    )


def test_sinistro_atribui_motorista_vigente(alocacao, veiculo, cliente):
    sinistro = Sinistro.objects.create(
        veiculo=veiculo, data=date(2026, 7, 10), envolvido="terceiro"
    )
    assert sinistro.cliente == cliente


def test_auxilio_disponivel_apos_7_dias_parado(alocacao, veiculo):
    sinistro = Sinistro.objects.create(
        veiculo=veiculo, data=date(2026, 7, 1), envolvido="associado", tipo="colisao"
    )
    hoje = date.today()
    Manutencao.objects.create(
        veiculo=veiculo,
        tipo="corretiva",
        data=hoje - timedelta(days=10),
        descricao="Funilaria dianteira",
        data_entrada=hoje - timedelta(days=10),
        sinistro=sinistro,
    )
    assert sinistro.dias_parado == 10
    assert sinistro.auxilio_disponivel is True
    AuxilioMotorista.objects.create(sinistro=sinistro, status="solicitado")
    assert sinistro.auxilio_disponivel is False


def test_roubo_nao_gera_auxilio(alocacao, veiculo):
    sinistro = Sinistro.objects.create(
        veiculo=veiculo, data=date(2026, 7, 1), envolvido="terceiro", tipo="roubo"
    )
    hoje = date.today()
    Manutencao.objects.create(
        veiculo=veiculo,
        tipo="corretiva",
        data=hoje,
        descricao="Vistoria pós-recuperação",
        data_entrada=hoje - timedelta(days=15),
        sinistro=sinistro,
    )
    assert sinistro.auxilio_disponivel is False


def test_repasse_de_manutencao_gera_cobranca(alocacao, veiculo, cliente):
    manutencao = Manutencao.objects.create(
        veiculo=veiculo,
        tipo="corretiva",
        data=date(2026, 7, 10),
        descricao="Retrovisor quebrado pelo cliente",
        custo_real=Decimal("120.00"),
        valor_cobrado_cliente=Decimal("150.00"),
        responsavel="cliente",
    )
    assert manutencao.diferenca == Decimal("30.00")
    cobranca = gerar_repasse(manutencao)
    assert cobranca.cliente == cliente
    assert cobranca.valor == Decimal("150.00")
    assert cobranca.classificacao_fiscal == "diverso"
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        gerar_repasse(manutencao)  # não duplica


def test_dias_parado_da_manutencao(veiculo, db):
    manutencao = Manutencao.objects.create(
        veiculo=veiculo,
        tipo="corretiva",
        data=date(2026, 7, 1),
        descricao="Câmbio",
        data_entrada=date(2026, 7, 1),
        data_saida=date(2026, 7, 9),
    )
    assert manutencao.dias_parado == 8


@pytest.fixture
def usuario_logado(client, django_user_model):
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    client.login(username="dono", password="senha-forte-123")
    return client


def test_telas_de_sinistros_renderizam(usuario_logado, alocacao, veiculo):
    sinistro = Sinistro.objects.create(
        veiculo=veiculo, data=date(2026, 7, 1), envolvido="associado", tipo="colisao"
    )
    hoje = date.today()
    Manutencao.objects.create(
        veiculo=veiculo,
        tipo="corretiva",
        data=hoje,
        descricao="Funilaria",
        data_entrada=hoje - timedelta(days=10),
        sinistro=sinistro,
    )
    assert usuario_logado.get("/sinistros/").status_code == 200
    assert usuario_logado.get("/sinistros/novo/").status_code == 200
    assert usuario_logado.get(f"/manutencao/historico/{veiculo.pk}/").status_code == 200
    assert usuario_logado.get(f"/manutencao/registrar/{veiculo.pk}/").status_code == 200
    assert usuario_logado.get("/").status_code == 200  # painel com auxílio a solicitar
