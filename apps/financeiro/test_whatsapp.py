"""Etapa 11 — mensagem de cobrança pronta para o WhatsApp."""

from datetime import date
from decimal import Decimal
from urllib.parse import unquote

import pytest

from apps.financeiro import whatsapp
from apps.financeiro.models import Cobranca
from apps.pessoas.models import Cliente


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(
        nome="Arlen Souza", cpf_cnpj="111.222.333-44", telefone="(31) 98888-7777"
    )


def cobranca_de(cliente, vencimento, status=Cobranca.Status.PENDENTE):
    return Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.ALUGUEL,
        descricao="Aluguel semanal",
        valor=Decimal("650.00"),
        vencimento=vencimento,
        status=status,
    )


def test_mensagem_em_dia_tem_nome_valor_e_pix(cliente, settings):
    settings.CHAVE_PIX = "financeiro@traderentacar.com.br"
    cobranca = cobranca_de(cliente, date(2026, 7, 22))
    texto = whatsapp.mensagem_cobranca(cobranca, hoje=date(2026, 7, 20))
    assert "Arlen" in texto
    assert "R$ 650,00" in texto
    assert "22/07" in texto
    assert "Pix: financeiro@traderentacar.com.br" in texto
    assert "venceu" not in texto


def test_mensagem_atrasada_diz_ha_quantos_dias(cliente, settings):
    settings.CHAVE_PIX = ""
    cobranca = cobranca_de(cliente, date(2026, 7, 15), Cobranca.Status.ATRASADO)
    texto = whatsapp.mensagem_cobranca(cobranca, hoje=date(2026, 7, 20))
    assert "venceu em 15/07" in texto
    assert "5 dias" in texto
    assert "Pix" not in texto  # sem chave configurada, sem linha de Pix


def test_link_usa_wa_me_com_ddi(cliente):
    cobranca = cobranca_de(cliente, date(2026, 7, 22))
    link = whatsapp.link_cobranca(cobranca, hoje=date(2026, 7, 20))
    assert link.startswith("https://wa.me/5531988887777?text=")
    assert "Arlen" in unquote(link)


def test_sem_telefone_nao_ha_link(db):
    cliente = Cliente.objects.create(nome="Sem Fone", cpf_cnpj="999.888.777-66")
    cobranca = cobranca_de(cliente, date(2026, 7, 22))
    assert whatsapp.link_cobranca(cobranca, hoje=date(2026, 7, 20)) == ""


def test_botao_aparece_na_tela_de_cobrancas(usuario_logado, cliente):
    cobranca_de(cliente, date(2026, 7, 22))
    html = usuario_logado.get("/financeiro/").content.decode()
    assert "cobrar no WhatsApp" in html
    assert "wa.me/5531988887777" in html


def test_cobranca_paga_nao_tem_botao(usuario_logado, cliente):
    cobranca_de(cliente, date(2026, 7, 22), Cobranca.Status.PAGO)
    html = usuario_logado.get("/financeiro/?status=todas").content.decode()
    assert "cobrar no WhatsApp" not in html
