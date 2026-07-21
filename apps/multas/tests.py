from datetime import date
from decimal import Decimal

import pytest

from apps.alocacoes.models import Alocacao
from apps.financeiro.models import Cobranca, NotaDebito
from apps.frota.models import Veiculo
from apps.multas.models import Multa, OrgaoAutuador
from apps.multas.services import (
    emitir_nota_debito,
    multas_com_fici_a_vencer,
    preencher_cliente_alocacao,
)
from apps.pessoas.models import Cliente


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_atual=50_000)


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


@pytest.fixture
def alocacao(veiculo, cliente):
    a = Alocacao(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=650,
        dia_vencimento=2,
        km_entrega=50_000,
    )
    a.save()
    return a


def test_senha_do_orgao_mascarada(db):
    orgao = OrgaoAutuador.objects.create(nome="PBH", senha="segredo123")
    assert orgao.senha_mascarada == "•" * 10
    assert "segredo" not in orgao.senha_mascarada


def test_preenche_cliente_vigente_na_data(alocacao, veiculo, cliente):
    multa = Multa(veiculo=veiculo, data=date(2026, 7, 10))
    preencher_cliente_alocacao(multa)
    assert multa.cliente_alocacao == cliente


def test_multa_sem_alocacao_fica_sem_cliente(veiculo):
    multa = Multa(veiculo=veiculo, data=date(2026, 6, 1))
    preencher_cliente_alocacao(multa)
    assert multa.cliente_alocacao is None


def test_alerta_fici_a_vencer(veiculo, cliente):
    Multa.objects.create(
        veiculo=veiculo,
        data=date(2026, 7, 1),
        descricao="Avançar sinal",
        fici_status=Multa.FICI.PENDENTE,
        fici_prazo=date(2026, 7, 20),
    )
    Multa.objects.create(
        veiculo=veiculo,
        data=date(2026, 7, 1),
        descricao="Longe",
        fici_status=Multa.FICI.PENDENTE,
        fici_prazo=date(2026, 12, 1),
    )
    a_vencer = multas_com_fici_a_vencer(hoje=date(2026, 7, 10))
    assert a_vencer.count() == 1
    assert a_vencer.first().descricao == "Avançar sinal"


def test_fici_indicado_nao_alerta(veiculo):
    Multa.objects.create(
        veiculo=veiculo,
        data=date(2026, 7, 1),
        fici_status=Multa.FICI.INDICADO,
        fici_prazo=date(2026, 7, 15),
    )
    assert multas_com_fici_a_vencer(hoje=date(2026, 7, 10)).count() == 0


def test_multa_nic_vinculada(veiculo):
    original = Multa.objects.create(veiculo=veiculo, data=date(2026, 7, 1), descricao="Original")
    nic = Multa.objects.create(
        veiculo=veiculo,
        data=date(2026, 9, 1),
        descricao="Não indicação de condutor",
        multa_origem=original,
        responsavel=Multa.Responsavel.EMPRESA,
        valor=Decimal("195.23"),
    )
    assert nic.eh_nic
    assert not original.eh_nic
    assert original.multas_nic.count() == 1


def test_emitir_nd_agrupa_multas_e_gera_cobranca(veiculo, cliente):
    m1 = Multa.objects.create(
        veiculo=veiculo,
        cliente_alocacao=cliente,
        data=date(2026, 7, 1),
        descricao="Multa 1",
        valor=Decimal("234.78"),
        repasse=Multa.Repasse.A_COBRAR,
    )
    m2 = Multa.objects.create(
        veiculo=veiculo,
        cliente_alocacao=cliente,
        data=date(2026, 7, 5),
        descricao="Multa 2",
        valor=Decimal("104.13"),
        repasse=Multa.Repasse.A_COBRAR,
    )
    nota = emitir_nota_debito(cliente, [m1, m2], data_emissao=date(2026, 7, 10))
    assert nota.numero == 1
    assert nota.total == Decimal("338.91")
    assert nota.cobranca.valor == Decimal("338.91")
    assert nota.cobranca.origem == Cobranca.Origem.NOTA_DEBITO
    m1.refresh_from_db()
    assert m1.repasse == Multa.Repasse.INCLUIDA_ND


def test_emitir_nd_sem_multas_retorna_none(cliente):
    assert emitir_nota_debito(cliente, []) is None
    assert NotaDebito.objects.count() == 0


@pytest.fixture
def usuario_logado(client, django_user_model):
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    client.login(username="dono", password="senha-forte-123")
    return client


def test_telas_de_multas_renderizam(usuario_logado, veiculo, cliente):
    Multa.objects.create(veiculo=veiculo, cliente_alocacao=cliente, data=date(2026, 7, 1))
    assert usuario_logado.get("/multas/").status_code == 200
    assert usuario_logado.get("/multas/nova/").status_code == 200
    assert usuario_logado.get("/multas/emitir-nd/").status_code == 200


def test_criar_multa_pela_tela_preenche_cliente(usuario_logado, alocacao, veiculo, cliente):
    resposta = usuario_logado.post(
        "/multas/nova/",
        {
            "veiculo": veiculo.pk,
            "data": "2026-07-10",
            "descricao": "Avançar sinal vermelho",
            "resultado": "em_aberto",
            "tipo_condutor": "cliente",
            "fici_status": "pendente",
            "pagamento": "pendente",
            "repasse": "a_cobrar",
            "responsavel": "cliente",
        },
    )
    assert resposta.status_code == 302
    multa = Multa.objects.get(descricao="Avançar sinal vermelho")
    assert multa.cliente_alocacao == cliente
