from datetime import date

import pytest

from apps.alocacoes.models import Alocacao
from apps.frota.models import Fornecedor, Veiculo
from apps.manutencao.models import Manutencao
from apps.pessoas.models import Cliente
from apps.sinistros.models import AuxilioMotorista, Sinistro
from apps.sinistros.services import (
    preencher_motorista,
    registrar_auxilio,
    sinistros_com_auxilio_a_solicitar,
)


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="RNA0F61", marca_modelo="Gol", km_atual=90_000)


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Jeferson", cpf_cnpj="222.333.444-55")


@pytest.fixture
def alocacao(veiculo, cliente):
    a = Alocacao(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 6, 1),
        valor_semanal=650,
        dia_vencimento=0,
        km_entrega=90_000,
    )
    a.save()
    return a


def test_preenche_motorista_vigente(alocacao, veiculo, cliente):
    sinistro = Sinistro(
        veiculo=veiculo, data=date(2026, 6, 15), envolvido=Sinistro.Envolvido.ASSOCIADO
    )
    preencher_motorista(sinistro)
    assert sinistro.motorista == cliente


def _colisao(veiculo):
    return Sinistro.objects.create(
        veiculo=veiculo,
        data=date(2026, 6, 10),
        tipo=Sinistro.Tipo.COLISAO,
        envolvido=Sinistro.Envolvido.ASSOCIADO,
    )


def _manutencao_parada(veiculo, entrada, saida=None):
    oficina = Fornecedor.objects.create(nome="By Car")
    return Manutencao.objects.create(
        veiculo=veiculo,
        tipo=Manutencao.Tipo.CORRETIVA,
        fornecedor=oficina,
        data=entrada,
        data_entrada=entrada,
        data_saida=saida,
        descricao="Funilaria após colisão",
    )


def test_auxilio_detectado_apos_7_dias(veiculo):
    sinistro = _colisao(veiculo)
    _manutencao_parada(veiculo, date(2026, 6, 11), date(2026, 6, 25))  # 14 dias
    candidatos = sinistros_com_auxilio_a_solicitar(hoje=date(2026, 6, 26))
    assert len(candidatos) == 1
    assert candidatos[0][0] == sinistro
    assert candidatos[0][1] == 14


def test_sem_auxilio_ate_7_dias(veiculo):
    _colisao(veiculo)
    _manutencao_parada(veiculo, date(2026, 6, 11), date(2026, 6, 16))  # 5 dias
    assert sinistros_com_auxilio_a_solicitar(hoje=date(2026, 6, 20)) == []


def test_auxilio_conta_dias_ate_hoje_se_em_aberto(veiculo):
    _colisao(veiculo)
    _manutencao_parada(veiculo, date(2026, 6, 11), saida=None)
    candidatos = sinistros_com_auxilio_a_solicitar(hoje=date(2026, 6, 30))
    assert candidatos[0][1] == 19


def test_nao_duplica_auxilio_ja_registrado(veiculo):
    sinistro = _colisao(veiculo)
    _manutencao_parada(veiculo, date(2026, 6, 11), date(2026, 6, 25))
    registrar_auxilio(sinistro, 14)
    assert sinistros_com_auxilio_a_solicitar(hoje=date(2026, 6, 26)) == []


def test_registrar_auxilio_cria_a_solicitar(veiculo):
    sinistro = _colisao(veiculo)
    aux = registrar_auxilio(sinistro, 14, valor=1518)
    assert aux.status == AuxilioMotorista.Status.A_SOLICITAR
    assert aux.dias_parado == 14


def test_manutencao_dias_parado(veiculo):
    m = _manutencao_parada(veiculo, date(2026, 6, 10), date(2026, 6, 20))
    assert m.dias_parado == 10


def test_manutencao_resultado(veiculo):
    oficina = Fornecedor.objects.create(nome="By Car")
    m = Manutencao.objects.create(
        veiculo=veiculo,
        tipo=Manutencao.Tipo.CORRETIVA,
        fornecedor=oficina,
        data=date(2026, 6, 10),
        descricao="Troca de peça",
        custo_real=300,
        valor_cobrado_cliente=500,
    )
    assert m.resultado == 200


@pytest.fixture
def usuario_logado(client, django_user_model):
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    client.login(username="dono", password="senha-forte-123")
    return client


def test_telas_de_sinistros_renderizam(usuario_logado, veiculo):
    _colisao(veiculo)
    assert usuario_logado.get("/sinistros/").status_code == 200
    assert usuario_logado.get("/sinistros/novo/").status_code == 200
    assert usuario_logado.get("/sinistros/auxilios/").status_code == 200


def test_criar_sinistro_pela_tela(usuario_logado, alocacao, veiculo, cliente):
    resposta = usuario_logado.post(
        "/sinistros/novo/",
        {
            "veiculo": veiculo.pk,
            "data": "2026-06-15",
            "tipo": "colisao",
            "envolvido": "associado",
            "responsabilidade": "cliente",
            "status": "aberto",
        },
    )
    assert resposta.status_code == 302
    sinistro = Sinistro.objects.get(veiculo=veiculo)
    assert sinistro.motorista == cliente
