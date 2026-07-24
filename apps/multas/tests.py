from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.financeiro import services as financeiro
from apps.frota.models import Veiculo
from apps.multas import services
from apps.multas.models import Multa, OrgaoAutuador
from apps.pessoas.models import Cliente


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


def test_credencial_do_orgao_criptografada_no_banco(db):
    orgao = OrgaoAutuador.objects.create(nome="PBH", senha="segredo-123")
    orgao.refresh_from_db()
    assert orgao.senha == "segredo-123"  # legível pela aplicação
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT senha FROM multas_orgaoautuador WHERE id = %s", [orgao.pk])
        bruto = cursor.fetchone()[0]
    assert "segredo-123" not in bruto  # cifrado no banco
    assert bruto.startswith("fernet:")


def test_multa_atribui_cliente_vigente(alocacao, veiculo, cliente):
    multa = Multa.objects.create(
        veiculo=veiculo, data_infracao=date(2026, 7, 10), descricao="Avançar sinal vermelho"
    )
    assert multa.cliente == cliente


def test_multa_em_substituto_atribui_cliente_da_troca(alocacao, cliente, db):
    substituto = Veiculo.objects.create(placa="RNB9J66", marca_modelo="Voyage")
    TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=date(2026, 7, 5),
        km_retirada=0,
    )
    multa = Multa.objects.create(
        veiculo=substituto, data_infracao=date(2026, 7, 10), descricao="Velocidade"
    )
    assert multa.cliente == cliente


def test_nic_cria_multa_vinculada_contra_a_empresa(alocacao, veiculo):
    multa = Multa.objects.create(
        veiculo=veiculo,
        data_infracao=date(2026, 7, 10),
        descricao="Avançar sinal",
        fici_prazo=date(2026, 8, 1),
    )
    nic = services.registrar_nic(multa, valor=Decimal("195.30"))
    multa.refresh_from_db()
    assert multa.fici_status == Multa.Fici.PRAZO_PERDIDO
    assert nic.multa_origem_nic == multa
    assert nic.responsavel == Multa.Responsavel.EMPRESA
    with pytest.raises(ValidationError):
        services.registrar_nic(multa)


def test_repasse_nao_se_aplica_para_advertencia_e_empresa(alocacao, veiculo):
    advertencia = Multa.objects.create(
        veiculo=veiculo,
        data_infracao=date(2026, 7, 10),
        resultado=Multa.Resultado.ADVERTENCIA,
    )
    assert advertencia.repasse == "Não se aplica"
    do_vendedor = Multa.objects.create(
        veiculo=veiculo,
        data_infracao=date(2026, 6, 1),
        valor=Decimal("104.13"),
        responsavel=Multa.Responsavel.VENDEDOR,
    )
    assert do_vendedor.repasse == "Não se aplica"


def test_gerar_nd_de_multas_e_receber(alocacao, veiculo, cliente):
    multa1 = Multa.objects.create(
        veiculo=veiculo,
        data_infracao=date(2026, 7, 5),
        descricao="Avançar sinal",
        valor=Decimal("234.78"),
    )
    multa2 = Multa.objects.create(
        veiculo=veiculo,
        data_infracao=date(2026, 7, 8),
        descricao="Velocidade",
        valor=Decimal("104.13"),
    )
    assert multa1.repasse == "A cobrar"
    nd = services.gerar_nd_de_multas(cliente, [multa1, multa2], date(2026, 7, 20))
    assert nd.total == Decimal("338.91")
    assert multa1.repasse == f"Incluída na ND {nd.numero:03d}"
    # não permite incluir de novo
    with pytest.raises(ValidationError):
        services.gerar_nd_de_multas(cliente, [multa1], date(2026, 7, 21))
    # recebimento da ND marca como recebido
    financeiro.registrar_recebimento(
        cliente,
        date(2026, 7, 22),
        Decimal("338.91"),
        "pix",
        [(nd.cobranca, Decimal("338.91"))],
    )
    multa1 = Multa.objects.get(pk=multa1.pk)
    assert multa1.repasse == "Recebido"


def test_alertas_fici(alocacao, veiculo):
    hoje = date(2026, 7, 20)
    Multa.objects.create(
        veiculo=veiculo,
        data_infracao=date(2026, 7, 1),
        descricao="Prazo próximo",
        fici_prazo=hoje + timedelta(days=3),
    )
    Multa.objects.create(
        veiculo=veiculo,
        data_infracao=date(2026, 7, 1),
        descricao="Prazo longe",
        fici_prazo=hoje + timedelta(days=30),
    )
    Multa.objects.create(
        veiculo=veiculo,
        data_infracao=date(2026, 7, 1),
        descricao="Já indicado",
        fici_prazo=hoje + timedelta(days=2),
        fici_status=Multa.Fici.INDICADO,
    )
    alertas = services.alertas_fici(hoje)
    assert [m.descricao for m in alertas] == ["Prazo próximo"]


@pytest.fixture
def usuario_logado(client, django_user_model):
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    client.login(username="dono", password="senha-forte-123")
    return client


def test_telas_de_multas_renderizam(usuario_logado, alocacao, veiculo, cliente):
    Multa.objects.create(
        veiculo=veiculo,
        data_infracao=date(2026, 7, 10),
        descricao="Avançar sinal",
        valor=Decimal("234.78"),
        fici_prazo=date(2026, 7, 25),
    )
    OrgaoAutuador.objects.create(nome="PBH", login="user", senha="secret")
    assert usuario_logado.get("/multas/").status_code == 200
    assert usuario_logado.get("/multas/nova/").status_code == 200
    assert usuario_logado.get(f"/multas/gerar-nd/?cliente={cliente.pk}").status_code == 200
    assert usuario_logado.get("/multas/orgaos/").status_code == 200
    assert usuario_logado.get("/").status_code == 200
