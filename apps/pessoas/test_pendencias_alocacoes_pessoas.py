"""Pendências das etapas 14/15 na frente pessoas: Recebi, Em aberto, condutor, dois carros."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.financeiro import services as financeiro
from apps.financeiro.models import Cobranca
from apps.frota.models import Veiculo
from apps.multas.models import Multa
from apps.pessoas.models import Cliente, CondutorAutorizado

HOJE = timezone.localdate()


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(
        nome="Diego Souza", cpf_cnpj="444.555.666-77", telefone="11977776666"
    )


def _alocar(cliente, placa):
    veiculo = Veiculo.objects.create(placa=placa, marca_modelo="Gol", km_atual=10_000)
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=2),
        valor_semanal=Decimal("600.00"),
        km_entrega=10_000,
    )


def _divida(cliente, alocacao=None, valor="600.00"):
    return Cobranca.objects.create(
        cliente=cliente,
        alocacao=alocacao,
        descricao="Semana",
        valor=Decimal(valor),
        vencimento=HOJE - timedelta(days=1),
    )


# ---------- Recebi nos cartões de Clientes ----------


def test_cartao_de_cliente_tem_recebi_so_quando_deve(usuario_logado, cliente):
    recebi = f"/financeiro/receber/?cliente={cliente.pk}"
    assert recebi not in usuario_logado.get("/clientes/").content.decode()
    _divida(cliente)
    assert recebi in usuario_logado.get("/clientes/").content.decode()


# ---------- ficha: bloco Em aberto ----------


def test_ficha_mostra_ultimo_pagamento_recebi_e_whatsapp(usuario_logado, cliente):
    alocacao = _alocar(cliente, "PES1A11")
    semana = _divida(cliente, alocacao, "1200.00")
    financeiro.registrar_recebimento(
        cliente, HOJE, Decimal("200.00"), "pix", [(semana, Decimal("200.00"))]
    )
    html = usuario_logado.get(f"/clientes/{cliente.pk}/").content.decode()
    assert f"Último pagamento: R$ 200,00 em {HOJE:%d/%m/%Y}" in html
    assert "Cobrar no WhatsApp" in html and "https://wa.me/5511977776666?text=" in html


def test_ficha_sem_divida_nao_mostra_cobrar(usuario_logado, cliente):
    html = usuario_logado.get(f"/clientes/{cliente.pk}/").content.decode()
    assert "Nenhum pagamento lançado ainda." in html
    assert "Cobrar no WhatsApp" not in html


# ---------- ficha: dois carros ao mesmo tempo e atalhos que voltam para ela ----------


def test_ficha_mostra_todas_as_alocacoes_ativas_com_voltar(usuario_logado, cliente):
    primeira = _alocar(cliente, "PES1A11")
    segunda = _alocar(cliente, "PES2B22")
    html = usuario_logado.get(f"/clientes/{cliente.pk}/").content.decode()
    ficha = f"/clientes/{cliente.pk}/"
    for alocacao in (primeira, segunda):
        assert alocacao.veiculo.placa in html
        assert f"/alocacoes/{alocacao.pk}/encerrar/?voltar={ficha}" in html
        assert f"/alocacoes/{alocacao.pk}/troca/?voltar={ficha}" in html


# ---------- condutor: CNH no formulário rápido e remover autorização ----------


def test_formulario_rapido_do_condutor_pede_a_cnh(usuario_logado, cliente):
    html = usuario_logado.get(f"/clientes/{cliente.pk}/").content.decode()
    assert 'name="cnh_numero"' in html
    usuario_logado.post(
        f"/clientes/{cliente.pk}/condutores/novo/",
        {"nome": "Rita", "cpf": "", "cnh_numero": "12345678900", "contato": ""},
    )
    assert CondutorAutorizado.objects.get(nome="Rita").cnh_numero == "12345678900"


def test_remover_condutor_com_confirmacao(usuario_logado, cliente):
    condutor = CondutorAutorizado.objects.create(nome="Rita", cliente=cliente)
    html = usuario_logado.get(f"/clientes/{cliente.pk}/").content.decode()
    assert f"/clientes/condutores/{condutor.pk}/remover/" in html
    assert "return confirm(" in html
    # GET não apaga nada
    usuario_logado.get(f"/clientes/condutores/{condutor.pk}/remover/")
    assert CondutorAutorizado.objects.filter(pk=condutor.pk).exists()
    resposta = usuario_logado.post(f"/clientes/condutores/{condutor.pk}/remover/")
    assert resposta.status_code == 302 and resposta.url == f"/clientes/{cliente.pk}/"
    assert not CondutorAutorizado.objects.filter(pk=condutor.pk).exists()


def test_condutor_que_aparece_em_multa_nao_e_removido(usuario_logado, cliente):
    alocacao = _alocar(cliente, "PES1A11")
    condutor = CondutorAutorizado.objects.create(nome="Rita", cliente=cliente)
    Multa.objects.create(
        veiculo=alocacao.veiculo,
        cliente=cliente,
        data_infracao=HOJE,
        condutor_autorizado=condutor,
    )
    resposta = usuario_logado.post(f"/clientes/condutores/{condutor.pk}/remover/", follow=True)
    assert CondutorAutorizado.objects.filter(pk=condutor.pk).exists()
    assert any("aparece em multa" in str(m) for m in resposta.context["messages"])


def test_condutor_com_cnh_longa_demais_explica_o_erro(usuario_logado, cliente):
    resposta = usuario_logado.post(
        f"/clientes/{cliente.pk}/condutores/novo/",
        {"nome": "Rita", "cpf": "", "cnh_numero": "1" * 30, "contato": ""},
        follow=True,
    )
    html = resposta.content.decode()
    assert not CondutorAutorizado.objects.filter(nome="Rita").exists()
    assert "Informe ao menos o nome" not in html
    assert "CNH — número:" in html and "20" in html
