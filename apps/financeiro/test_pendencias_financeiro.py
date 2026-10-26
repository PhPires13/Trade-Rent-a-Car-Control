"""Pendências da etapa 15 na frente financeiro.

Teclado numérico e rótulos ligados (caução, ND, encargo), botão do DAS em
.xlsx, encargo de ND cancelada, WhatsApp da semana proporcional, crédito na
tela da caução, aviso de cliente sem telefone e crédito que voltaria ao crédito.
"""

import csv
import io
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import unquote

import pytest
from django.contrib.messages import get_messages

from apps.alocacoes.models import Alocacao
from apps.financeiro import services, whatsapp
from apps.financeiro.models import Cobranca, MovimentacaoCaucao, MovimentoCredito, Recebimento
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente

HOJE = date.today()


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(
        nome="Arlen Souza", cpf_cnpj="111.222.333-44", telefone="(31) 98888-7777"
    )


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_atual=50_000)


@pytest.fixture
def alocacao(veiculo, cliente):
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=30),
        valor_semanal=Decimal("700.00"),
        dia_vencimento=2,
        km_entrega=50_000,
    )


def _atrasada(cliente, **kw):
    kw.setdefault("origem", Cobranca.Origem.ALUGUEL)
    kw.setdefault("descricao", "Aluguel semanal QXQ6C10")
    kw.setdefault("valor", Decimal("700.00"))
    kw.setdefault("vencimento", HOJE - timedelta(days=10))
    kw.setdefault("status", Cobranca.Status.ATRASADO)
    return Cobranca.objects.create(cliente=cliente, **kw)


def _mensagens(resposta):
    return [str(m) for m in get_messages(resposta.wsgi_request)]


# ---------- teclado numérico e rótulos ligados ----------


def test_caucao_tem_teclado_numerico_e_rotulos_ligados(usuario_logado, alocacao):
    caucao = services.abrir_caucao(alocacao)
    html = usuario_logado.get(f"/financeiro/caucoes/{caucao.pk}/").content.decode()
    assert 'inputmode="decimal"' in html
    for campo in ("tipo", "valor", "data", "forma", "cobranca", "observacoes"):
        assert f'for="caucao-{campo}"' in html and f'id="caucao-{campo}"' in html


def test_nd_tem_teclado_numerico_e_rotulos_ligados(usuario_logado, cliente):
    html = usuario_logado.get("/financeiro/nd/nova/").content.decode()
    assert 'inputmode="decimal"' in html
    for campo in ("cliente", "emissao", "vencimento", "observacoes"):
        assert f'for="nd-{campo}"' in html and f'id="nd-{campo}"' in html
    assert "Cliente *" not in html  # obrigatório sem marca; opcional ganha "(opcional)"


def test_encargo_tem_teclado_numerico_e_rotulo_ligado(usuario_logado, cliente):
    atrasada = _atrasada(cliente)
    html = usuario_logado.get("/financeiro/?status=abertas").content.decode()
    assert f'for="encargo-{atrasada.pk}"' in html
    assert f'id="encargo-{atrasada.pk}"' in html
    trecho = html.split(f'id="encargo-{atrasada.pk}"')[1].split(">")[0]
    assert 'inputmode="decimal"' in trecho


# ---------- DAS ----------


def test_botao_do_das_baixa_xlsx(usuario_logado, db):
    html = usuario_logado.get("/financeiro/das/?mes=2026-07").content.decode()
    assert "exportar=xlsx" in html
    assert "exportar=csv" not in html
    resposta = usuario_logado.get("/financeiro/das/?mes=2026-07&exportar=xlsx")
    assert resposta.status_code == 200
    assert "spreadsheetml" in resposta["Content-Type"]


def test_planilha_do_das_avisa_que_caucao_nao_e_receita(usuario_logado, db):
    texto = usuario_logado.get("/financeiro/das/?mes=2026-07&exportar=csv").content.decode()
    grupos = [linha[0] for linha in csv.reader(io.StringIO(texto.lstrip("﻿")), delimiter=";")]
    assert "Caução recebida (não é receita — é do cliente)" in grupos


# ---------- encargo de ND cancelada ----------


def _nd_com_encargo(cliente, pago=False):
    nd = services.emitir_nota_debito(cliente, HOJE - timedelta(days=20), [("Multa", "300.00")])
    cobranca_nd = nd.cobranca
    encargo = _atrasada(
        cliente,
        origem=Cobranca.Origem.ENCARGO,
        descricao=f"Encargo por atraso — {cobranca_nd.descricao}",
        valor=Decimal("30.00"),
        cobranca_origem=cobranca_nd,
        status=Cobranca.Status.PENDENTE,
    )
    if pago:
        services.registrar_recebimento(
            cliente, HOJE, Decimal("10.00"), Recebimento.Forma.PIX, [(encargo, Decimal("10.00"))]
        )
    return cobranca_nd, encargo


def test_cancelar_nd_cancela_o_encargo_sem_pagamento(usuario_logado, cliente):
    cobranca_nd, encargo = _nd_com_encargo(cliente)
    resposta = usuario_logado.post(f"/financeiro/cobranca/{cobranca_nd.pk}/cancelar/")
    encargo.refresh_from_db()
    assert encargo.status == Cobranca.Status.CANCELADA
    textos = _mensagens(resposta)
    assert any("encargo por atraso dela também foi cancelado" in t for t in textos)
    assert not any("continua como está" in t for t in textos)


def test_cancelar_nd_mantem_encargo_ja_pago_em_parte(usuario_logado, cliente):
    cobranca_nd, encargo = _nd_com_encargo(cliente, pago=True)
    resposta = usuario_logado.post(f"/financeiro/cobranca/{cobranca_nd.pk}/cancelar/")
    encargo.refresh_from_db()
    assert encargo.status == Cobranca.Status.ATRASADO  # vencido, pago em parte
    assert any("continua como está" in t for t in _mensagens(resposta))


# ---------- WhatsApp da semana proporcional ----------


def test_whatsapp_da_semana_proporcional_diz_os_dias(alocacao, cliente):
    inicio = HOJE + timedelta(days=1)
    cobranca = _atrasada(
        cliente,
        alocacao=alocacao,
        vencimento=inicio,
        periodo_inicio=inicio,
        periodo_fim=inicio + timedelta(days=2),
        valor=Decimal("300.00"),
        status=Cobranca.Status.PENDENTE,
    )
    texto = unquote(whatsapp.link_cobranca(cobranca, HOJE))
    fim = inicio + timedelta(days=2)
    assert f"O aluguel de 3 dias ({inicio:%d/%m} a {fim:%d/%m}) do carro QXQ6C10" in texto
    assert "da semana" not in texto


def test_whatsapp_da_semana_cheia_continua_igual(alocacao, cliente):
    inicio = HOJE + timedelta(days=1)
    cobranca = _atrasada(
        cliente,
        alocacao=alocacao,
        vencimento=inicio,
        periodo_inicio=inicio,
        periodo_fim=inicio + timedelta(days=6),
        status=Cobranca.Status.PENDENTE,
    )
    assert "O aluguel da semana do carro QXQ6C10" in unquote(whatsapp.link_cobranca(cobranca, HOJE))


def test_whatsapp_de_um_dia(alocacao, cliente):
    inicio = HOJE + timedelta(days=1)
    cobranca = _atrasada(
        cliente,
        alocacao=alocacao,
        vencimento=inicio,
        periodo_inicio=inicio,
        periodo_fim=inicio,
        valor=Decimal("100.00"),
        status=Cobranca.Status.PENDENTE,
    )
    assert f"o aluguel de 1 dia ({inicio:%d/%m})" in whatsapp.o_que_e(cobranca)


# ---------- crédito na tela da caução ----------


def test_tela_da_caucao_mostra_o_credito_do_cliente(usuario_logado, alocacao, cliente):
    caucao = services.abrir_caucao(alocacao)
    url = f"/financeiro/caucoes/{caucao.pk}/"
    assert "data-credito-cliente" not in usuario_logado.get(url).content.decode()
    MovimentoCredito.objects.create(
        cliente=cliente, tipo="entrada", valor=Decimal("200.00"), data=HOJE
    )
    resposta = usuario_logado.get(url)
    assert resposta.context["credito"] == Decimal("200.00")
    html = resposta.content.decode()
    assert "data-credito-cliente" in html
    assert "R$ 200,00 de crédito" in html
    assert f"/financeiro/cliente/{cliente.pk}/" in html


# ---------- cobranças: cliente sem telefone e espaço entre botões ----------


def test_cobranca_de_cliente_sem_telefone_avisa(usuario_logado, cliente):
    cliente.telefone = ""
    cliente.save(update_fields=["telefone"])
    _atrasada(cliente)
    html = usuario_logado.get("/financeiro/?status=abertas").content.decode()
    assert "Sem WhatsApp: cliente sem telefone" in html
    assert f"/clientes/{cliente.pk}/editar/" in html


def test_cobranca_com_telefone_nao_mostra_aviso(usuario_logado, cliente):
    _atrasada(cliente)
    html = usuario_logado.get("/financeiro/?status=abertas").content.decode()
    assert "cliente sem telefone" not in html
    assert "space-y-2" in html  # botões empilhados com 8px entre eles


# ---------- pagar com crédito ----------


def test_pagar_com_credito_o_valor_exato_continua_valendo(cliente):
    atrasada = _atrasada(cliente, valor=Decimal("100.00"))
    MovimentoCredito.objects.create(
        cliente=cliente, tipo="entrada", valor=Decimal("500.00"), data=HOJE
    )
    services.registrar_recebimento(
        cliente, HOJE, Decimal("100.00"), Recebimento.Forma.CREDITO, [(atrasada, "100.00")]
    )
    atrasada.refresh_from_db()
    assert atrasada.status == Cobranca.Status.PAGO
    assert MovimentoCredito.saldo_do_cliente(cliente) == Decimal("400.00")


def test_extrato_usa_o_botao_recebi(usuario_logado, cliente):
    html = usuario_logado.get(f"/financeiro/cliente/{cliente.pk}/").content.decode()
    assert "✓ Recebi" in html
    assert "Registrar pagamento" not in html


def test_movimentacao_caucao_continua_funcionando(usuario_logado, alocacao):
    caucao = services.abrir_caucao(alocacao)
    usuario_logado.post(
        f"/financeiro/caucoes/{caucao.pk}/",
        {
            "tipo": "recebimento",
            "valor": "1.500,00",
            "data": HOJE.isoformat(),
            "forma": "pix",
            "token": "t1",
        },
    )
    assert MovimentacaoCaucao.objects.filter(caucao=caucao).count() == 1


# ---------- revisão adversarial ----------


def test_cancelar_nd_com_encargo_atrasado_tira_o_cliente_da_inadimplencia(usuario_logado, cliente):
    """O encargo vencido há tempo também deixaria o cliente inadimplente: a
    inadimplência é recalculada só depois que o encargo é cancelado junto."""
    cobranca_nd, encargo = _nd_com_encargo(cliente)
    Cobranca.objects.filter(pk__in=[cobranca_nd.pk, encargo.pk]).update(
        status=Cobranca.Status.ATRASADO, vencimento=HOJE - timedelta(days=20)
    )
    cliente.status = Cliente.Status.INADIMPLENTE
    cliente.save(update_fields=["status"])
    usuario_logado.post(f"/financeiro/cobranca/{cobranca_nd.pk}/cancelar/")
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.ATIVO


def test_cancelar_nd_com_dois_encargos_conta_no_plural(usuario_logado, cliente):
    cobranca_nd, _ = _nd_com_encargo(cliente)
    _atrasada(
        cliente,
        origem=Cobranca.Origem.ENCARGO,
        descricao="Encargo por atraso (2)",
        valor=Decimal("15.00"),
        cobranca_origem=cobranca_nd,
        status=Cobranca.Status.PENDENTE,
    )
    resposta = usuario_logado.post(f"/financeiro/cobranca/{cobranca_nd.pk}/cancelar/")
    assert cobranca_nd.encargos.exclude(status=Cobranca.Status.CANCELADA).count() == 0
    assert any("Os 2 encargos por atraso dela" in t for t in _mensagens(resposta))


def test_credito_na_caucao_com_cobranca_aberta_leva_ao_recebimento(
    usuario_logado, alocacao, cliente
):
    caucao = services.abrir_caucao(alocacao)
    MovimentoCredito.objects.create(
        cliente=cliente, tipo="entrada", valor=Decimal("200.00"), data=HOJE
    )
    url = f"/financeiro/caucoes/{caucao.pk}/"
    assert "Usar o crédito nas cobranças" not in usuario_logado.get(url).content.decode()
    _atrasada(cliente)
    html = usuario_logado.get(url).content.decode()
    assert "Usar o crédito nas cobranças" in html
    assert f"/financeiro/receber/?cliente={cliente.pk}" in html


def test_cobranca_de_telefone_sem_digitos_tambem_avisa(usuario_logado, cliente):
    cliente.telefone = "-"
    cliente.save(update_fields=["telefone"])
    _atrasada(cliente)
    html = usuario_logado.get("/financeiro/?status=abertas").content.decode()
    assert "Sem WhatsApp: telefone incompleto" in html


def test_cobranca_paga_de_cliente_sem_telefone_nao_avisa(usuario_logado, cliente):
    cliente.telefone = ""
    cliente.save(update_fields=["telefone"])
    _atrasada(cliente, status=Cobranca.Status.PAGO)
    html = usuario_logado.get("/financeiro/?status=todas").content.decode()
    assert "Sem WhatsApp" not in html
