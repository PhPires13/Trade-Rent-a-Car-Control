"""Cancelamento de cobranças automáticas ("não cobrar") — etapa 9."""

from datetime import date
from decimal import Decimal

import pytest

from apps.financeiro.models import Cobranca, Recebimento
from apps.financeiro.services import registrar_recebimento
from apps.pessoas.models import Cliente


@pytest.fixture
def usuario_logado(client, django_user_model):
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    client.login(username="dono", password="senha-forte-123")
    return client


@pytest.fixture
def cliente_arlen(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


def cobranca_de(cliente, origem, valor="500.00"):
    return Cobranca.objects.create(
        cliente=cliente,
        origem=origem,
        descricao="Excedente de km QXQ6C10 07/2026",
        valor=Decimal(valor),
        vencimento=date(2026, 7, 22),
    )


def test_cancela_excedente_sem_pagamento(usuario_logado, cliente_arlen):
    cobranca = cobranca_de(cliente_arlen, Cobranca.Origem.EXCEDENTE_KM)
    resposta = usuario_logado.post(f"/financeiro/cobranca/{cobranca.pk}/cancelar/", follow=True)
    cobranca.refresh_from_db()
    assert cobranca.status == Cobranca.Status.CANCELADA
    mensagens = [str(m) for m in resposta.context["messages"]]
    assert any("não será cobrada" in m for m in mensagens)


def test_nao_cancela_cobranca_de_aluguel(usuario_logado, cliente_arlen):
    cobranca = cobranca_de(cliente_arlen, Cobranca.Origem.ALUGUEL)
    usuario_logado.post(f"/financeiro/cobranca/{cobranca.pk}/cancelar/")
    cobranca.refresh_from_db()
    assert cobranca.status != Cobranca.Status.CANCELADA


def test_nao_cancela_com_pagamento_aplicado(usuario_logado, cliente_arlen):
    cobranca = cobranca_de(cliente_arlen, Cobranca.Origem.EXCEDENTE_KM)
    registrar_recebimento(
        cliente=cliente_arlen,
        data=date(2026, 7, 20),
        valor=Decimal("100.00"),
        forma=Recebimento.Forma.PIX,
        aplicacoes=[(cobranca, Decimal("100.00"))],
    )
    usuario_logado.post(f"/financeiro/cobranca/{cobranca.pk}/cancelar/")
    cobranca.refresh_from_db()
    assert cobranca.status != Cobranca.Status.CANCELADA


def test_nao_cancela_cobranca_judicial(usuario_logado, cliente_arlen):
    cobranca = cobranca_de(cliente_arlen, Cobranca.Origem.EXCEDENTE_KM)
    cobranca.status = Cobranca.Status.JUDICIAL
    cobranca.save(update_fields=["status"])
    usuario_logado.post(f"/financeiro/cobranca/{cobranca.pk}/cancelar/")
    cobranca.refresh_from_db()
    assert cobranca.status == Cobranca.Status.JUDICIAL


def test_sugestao_de_encargo_volta_apos_cancelar_o_encargo(usuario_logado, cliente_arlen):
    from apps.financeiro import services

    atrasada = Cobranca.objects.create(
        cliente=cliente_arlen,
        origem=Cobranca.Origem.ALUGUEL,
        descricao="Aluguel semanal",
        valor=Decimal("650.00"),
        vencimento=date(2026, 7, 1),
        status=Cobranca.Status.ATRASADO,
    )
    encargo = services.aplicar_encargo(atrasada, Decimal("65.00"), hoje=date(2026, 7, 10))
    assert "Aplicar" not in usuario_logado.get("/financeiro/").content.decode()
    usuario_logado.post(f"/financeiro/cobranca/{encargo.pk}/cancelar/")
    # cancelado o encargo, o dono volta a poder aplicar outro pela tela
    assert "Aplicar" in usuario_logado.get("/financeiro/").content.decode()


def test_get_nao_cancela(usuario_logado, cliente_arlen):
    cobranca = cobranca_de(cliente_arlen, Cobranca.Origem.EXCEDENTE_KM)
    usuario_logado.get(f"/financeiro/cobranca/{cobranca.pk}/cancelar/")
    cobranca.refresh_from_db()
    assert cobranca.status != Cobranca.Status.CANCELADA


def test_cancelada_sai_do_saldo_devedor_do_cliente(usuario_logado, cliente_arlen):
    cobranca = cobranca_de(cliente_arlen, Cobranca.Origem.EXCEDENTE_KM)
    usuario_logado.post(f"/financeiro/cobranca/{cobranca.pk}/cancelar/")
    abertas = cliente_arlen.cobrancas.filter(
        status__in=[
            Cobranca.Status.PENDENTE,
            Cobranca.Status.PARCIAL,
            Cobranca.Status.ATRASADO,
        ]
    )
    assert not abertas.exists()


def test_botao_nao_cobrar_aparece_para_excedente(usuario_logado, cliente_arlen):
    cobranca_de(cliente_arlen, Cobranca.Origem.EXCEDENTE_KM)
    html = usuario_logado.get("/financeiro/").content.decode()
    assert "não cobrar" in html


def test_botao_nao_cobrar_nao_aparece_para_aluguel(usuario_logado, cliente_arlen):
    cobranca_de(cliente_arlen, Cobranca.Origem.ALUGUEL)
    html = usuario_logado.get("/financeiro/").content.decode()
    assert "não cobrar" not in html
