"""Tela de receber (propostas de UX nº 1-A e nº 5-A, "Recebi").

Cobre: crédito como botão (e não forma de pagamento), trava contra toque
duplo, resumo pós-lançamento com recibo no WhatsApp, desfazer no mesmo dia
(devolve saldo, crédito, caução e inadimplência) e os atalhos "Recebi".
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.contrib.messages import get_messages
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.financeiro import services
from apps.financeiro.models import (
    Cobranca,
    MovimentacaoCaucao,
    MovimentoCredito,
    Recebimento,
)
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente

URL = "/financeiro/receber/"


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(
        nome="João Silva", cpf_cnpj="111.222.333-44", telefone="(11) 98888-7777"
    )


@pytest.fixture
def atrasada(cliente):
    cobranca = Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.ALUGUEL,
        descricao="Aluguel semanal QXQ6C10 — venc. 01/07/2026",
        valor=Decimal("650.00"),
        vencimento=date.today() - timedelta(days=10),
        status=Cobranca.Status.ATRASADO,
    )
    services.atualizar_inadimplencia(cliente)
    return cobranca


@pytest.fixture
def alocacao(cliente):
    veiculo = Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol")
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
        caucao_valor=Decimal("1000.00"),
    )


def pagar(client, cliente, cobranca=None, valor="700,00", aplicar="650,00", **extra):
    dados = {
        "cliente": cliente.pk,
        "valor": valor,
        "data": date.today().isoformat(),
        "forma": "pix",
        "sobra_destino": "credito",
        **extra,
    }
    if cobranca is not None:
        dados[f"aplicar_{cobranca.pk}"] = aplicar
    return client.post(URL, dados)


def mensagens(resposta):
    return [str(m) for m in get_messages(resposta.wsgi_request)]


# ---------- formulário ----------


def test_credito_nao_e_forma_de_pagamento(usuario_logado, cliente, atrasada):
    resposta = usuario_logado.get(f"{URL}?cliente={cliente.pk}")
    formas = [valor for valor, _ in resposta.context["formas"]]
    assert "credito" not in formas
    assert "pix" in formas


def test_botao_usar_credito_so_com_credito(usuario_logado, cliente, atrasada):
    html = usuario_logado.get(f"{URL}?cliente={cliente.pk}").content.decode()
    assert "Usar o crédito" not in html
    MovimentoCredito.objects.create(
        cliente=cliente, tipo="entrada", valor=Decimal("50.00"), data=date.today()
    )
    html = usuario_logado.get(f"{URL}?cliente={cliente.pk}").content.decode()
    assert "Usar o crédito de R$ 50,00" in html


def test_pagar_com_credito_pela_tela(usuario_logado, cliente, atrasada):
    MovimentoCredito.objects.create(
        cliente=cliente, tipo="entrada", valor=Decimal("650.00"), data=date.today()
    )
    pagar(usuario_logado, cliente, atrasada, valor="650,00", forma="credito")
    recebimento = Recebimento.objects.get()
    assert recebimento.forma == "credito"
    assert MovimentoCredito.saldo_do_cliente(cliente) == Decimal("0.00")
    atrasada.refresh_from_db()
    assert atrasada.status == Cobranca.Status.PAGO


def test_campos_de_dinheiro_abrem_teclado_numerico(usuario_logado, cliente, atrasada):
    html = usuario_logado.get(f"{URL}?cliente={cliente.pk}").content.decode()
    assert 'for="valor"' in html and 'id="valor"' in html
    assert f'for="aplicar_{atrasada.pk}"' in html and f'id="aplicar_{atrasada.pk}"' in html
    assert html.count('inputmode="decimal"') == 2  # valor + 1 cobrança


def test_forma_invalida_e_recusada(cliente, atrasada):
    with pytest.raises(services.ValidationError):
        services.registrar_recebimento(cliente, date.today(), Decimal("10"), "cheque", [])


def test_atalho_cobranca_preenche_valor_e_aplicacao(usuario_logado, cliente, atrasada):
    resposta = usuario_logado.get(f"{URL}?cliente={cliente.pk}&cobranca={atrasada.pk}")
    html = resposta.content.decode()
    assert 'value="650,00"' in html
    assert resposta.context["linhas"] == [(atrasada, "650,00")]


def test_atalho_com_cobranca_de_outro_cliente_e_ignorado(usuario_logado, cliente, atrasada):
    outro = Cliente.objects.create(nome="Maria", cpf_cnpj="999.888.777-66")
    resposta = usuario_logado.get(f"{URL}?cliente={outro.pk}&cobranca={atrasada.pk}")
    assert resposta.status_code == 200
    assert resposta.context["post"] is None


# ---------- trava contra lançamento repetido ----------


def test_toque_duplo_nao_lanca_em_dobro(usuario_logado, cliente, atrasada):
    primeira = pagar(usuario_logado, cliente, atrasada)
    # segundo toque: a cobrança já saiu da lista; só sobra o valor inteiro
    segunda = pagar(usuario_logado, cliente, None)
    assert Recebimento.objects.count() == 1
    recebimento = Recebimento.objects.get()
    assert primeira.url.endswith(f"recibo={recebimento.pk}")
    assert segunda.url.endswith(f"recibo={recebimento.pk}")
    assert any("já foi lançado agora há pouco" in m for m in mensagens(segunda))
    assert MovimentoCredito.saldo_do_cliente(cliente) == Decimal("50.00")  # não 750


def test_mesmo_valor_depois_de_2_minutos_e_aceito(usuario_logado, cliente, atrasada):
    pagar(usuario_logado, cliente, atrasada)
    Recebimento.history.update(history_date=timezone.now() - timedelta(minutes=3))
    pagar(usuario_logado, cliente, None)
    assert Recebimento.objects.count() == 2


def test_valor_diferente_nao_e_repetido(usuario_logado, cliente, atrasada):
    pagar(usuario_logado, cliente, atrasada)
    pagar(usuario_logado, cliente, None, valor="100,00")
    assert Recebimento.objects.count() == 2


# ---------- resumo pós-lançamento e recibo ----------


def test_resumo_diz_o_que_quitou_e_que_ficou_em_dia(usuario_logado, cliente, atrasada):
    resposta = pagar(usuario_logado, cliente, atrasada)
    html = usuario_logado.get(resposta.url).content.decode()
    assert "Pagamento lançado" in html
    assert "Quitou:" in html and atrasada.descricao in html
    assert "Sobrou R$ 50,00" in html
    assert "João Silva está em dia ✓" in html
    assert "https://wa.me/5511988887777?text=" in html
    assert "Desfazer este lançamento" in html
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.ATIVO


def test_resumo_avisa_quando_continua_devendo(usuario_logado, cliente, atrasada):
    resposta = pagar(usuario_logado, cliente, atrasada, valor="100,00", aplicar="100,00")
    html = usuario_logado.get(resposta.url).content.decode()
    assert "Abateu" in html
    assert "continua inadimplente" in html and "R$ 550,00" in html


def test_mensagem_do_recibo(cliente, atrasada):
    from apps.financeiro import recibo

    recebimento = services.registrar_recebimento(
        cliente, date.today(), Decimal("700.00"), "pix", [(atrasada, Decimal("650.00"))]
    )
    texto = recibo.mensagem_recibo(services.resumo_recebimento(recebimento))
    assert texto.startswith("Olá, João! Recebemos seu pagamento de R$ 700,00 (Pix)")
    assert "Quitado: " + atrasada.descricao in texto
    assert "R$ 50,00 ficaram guardados como crédito" in texto
    assert "em dia" in texto


def test_sem_telefone_nao_tem_botao_de_recibo(usuario_logado, cliente, atrasada):
    Cliente.objects.filter(pk=cliente.pk).update(telefone="")
    resposta = pagar(usuario_logado, cliente, atrasada)
    html = usuario_logado.get(resposta.url).content.decode()
    assert "wa.me" not in html
    assert "Sem telefone no cadastro" in html


def test_recibo_de_outro_cliente_nao_aparece(usuario_logado, cliente, atrasada):
    resposta = pagar(usuario_logado, cliente, atrasada)
    outro = Cliente.objects.create(nome="Maria", cpf_cnpj="999.888.777-66")
    recibo_id = resposta.url.split("recibo=")[1]
    html = usuario_logado.get(f"{URL}?cliente={outro.pk}&recibo={recibo_id}").content.decode()
    assert "Pagamento lançado" not in html


# ---------- desfazer no mesmo dia ----------


def desfazer(client, recebimento):
    return client.post(f"/financeiro/recebimento/{recebimento.pk}/desfazer/")


def test_desfazer_devolve_saldo_credito_e_inadimplencia(usuario_logado, cliente, atrasada):
    pagar(usuario_logado, cliente, atrasada)
    recebimento = Recebimento.objects.get()
    resposta = desfazer(usuario_logado, recebimento)
    assert resposta.url == f"{URL}?cliente={cliente.pk}"
    assert not Recebimento.objects.exists()
    assert MovimentoCredito.saldo_do_cliente(cliente) == Decimal("0.00")
    atrasada.refresh_from_db()
    assert atrasada.status == Cobranca.Status.ATRASADO
    assert atrasada.saldo == Decimal("650.00")
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.INADIMPLENTE
    assert any("desfeito" in m for m in mensagens(resposta))
    # o histórico guarda o lançamento apagado
    assert Recebimento.history.filter(history_type="-").count() == 1


def test_desfazer_uso_de_credito_devolve_o_credito(cliente, atrasada):
    MovimentoCredito.objects.create(
        cliente=cliente, tipo="entrada", valor=Decimal("650.00"), data=date.today()
    )
    recebimento = services.registrar_recebimento(
        cliente, date.today(), Decimal("650.00"), "credito", [(atrasada, Decimal("650.00"))]
    )
    assert MovimentoCredito.saldo_do_cliente(cliente) == Decimal("0.00")
    services.desfazer_recebimento(recebimento)
    assert MovimentoCredito.saldo_do_cliente(cliente) == Decimal("650.00")


def test_desfazer_remove_reforco_de_caucao(cliente, atrasada, alocacao):
    caucao = services.abrir_caucao(alocacao, Decimal("1000.00"), date.today())
    recebimento = services.registrar_recebimento(
        cliente,
        date.today(),
        Decimal("700.00"),
        "pix",
        [(atrasada, Decimal("650.00"))],
        sobra_destino="caucao",
    )
    assert caucao.saldo == Decimal("1050.00")
    resumo = services.resumo_recebimento(recebimento)
    assert resumo["sobra"] == (Decimal("50.00"), "caucao")
    services.desfazer_recebimento(recebimento)
    assert caucao.saldo == Decimal("1000.00")
    assert not caucao.movimentacoes.filter(tipo=MovimentacaoCaucao.Tipo.REFORCO).exists()


def test_desfazer_so_no_mesmo_dia(usuario_logado, cliente, atrasada):
    pagar(usuario_logado, cliente, atrasada)
    recebimento = Recebimento.objects.get()
    Recebimento.history.update(history_date=timezone.now() - timedelta(days=1))
    resposta = desfazer(usuario_logado, recebimento)
    assert Recebimento.objects.exists()
    assert any("mesmo dia" in m for m in mensagens(resposta))
    html = usuario_logado.get(f"{URL}?cliente={cliente.pk}&recibo={recebimento.pk}")
    assert "Desfazer este lançamento" not in html.content.decode()


def test_nao_desfaz_se_o_credito_gerado_ja_foi_usado(cliente, atrasada):
    recebimento = services.registrar_recebimento(
        cliente, date.today(), Decimal("700.00"), "pix", [(atrasada, Decimal("650.00"))]
    )
    MovimentoCredito.objects.create(
        cliente=cliente, tipo="uso", valor=Decimal("50.00"), data=date.today()
    )
    with pytest.raises(services.ValidationError, match="já foi usado"):
        services.desfazer_recebimento(recebimento)
    assert Recebimento.objects.filter(pk=recebimento.pk).exists()


def test_desfazer_cobranca_judicial_volta_a_ser_judicial(cliente):
    judicial = Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.ALUGUEL,
        descricao="Aluguel semanal (acordo)",
        valor=Decimal("650.00"),
        vencimento=date.today() - timedelta(days=30),
        status=Cobranca.Status.JUDICIAL,
    )
    recebimento = services.registrar_recebimento(
        cliente, date.today(), Decimal("650.00"), "pix", [(judicial, Decimal("650.00"))]
    )
    judicial.refresh_from_db()
    assert judicial.status == Cobranca.Status.PAGO
    services.desfazer_recebimento(recebimento)
    judicial.refresh_from_db()
    assert judicial.status == Cobranca.Status.JUDICIAL
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.INADIMPLENTE


def test_desfazer_por_get_nao_apaga(usuario_logado, cliente, atrasada):
    pagar(usuario_logado, cliente, atrasada)
    recebimento = Recebimento.objects.get()
    usuario_logado.get(f"/financeiro/recebimento/{recebimento.pk}/desfazer/")
    assert Recebimento.objects.exists()


def test_extrato_mostra_desfazer_so_para_lancamento_de_hoje(usuario_logado, cliente, atrasada):
    pagar(usuario_logado, cliente, atrasada)
    url = f"/financeiro/cliente/{cliente.pk}/"
    assert "desfazer (lançado hoje)" in usuario_logado.get(url).content.decode()
    Recebimento.history.update(history_date=timezone.now() - timedelta(days=1))
    assert "desfazer (lançado hoje)" not in usuario_logado.get(url).content.decode()


# ---------- atalhos "Recebi" (proposta nº 5-A) ----------


def test_recebi_na_lista_de_cobrancas(usuario_logado, cliente, atrasada):
    html = usuario_logado.get("/financeiro/").content.decode()
    assert f"{URL}?cliente={cliente.pk}&cobranca={atrasada.pk}" in html
    assert "✓ Recebi" in html


def test_recebi_no_extrato_so_para_o_que_falta(usuario_logado, cliente, atrasada):
    url = f"/financeiro/cliente/{cliente.pk}/"
    assert f"cobranca={atrasada.pk}" in usuario_logado.get(url).content.decode()
    services.registrar_recebimento(
        cliente, date.today(), Decimal("650.00"), "pix", [(atrasada, Decimal("650.00"))]
    )
    assert f"cobranca={atrasada.pk}" not in usuario_logado.get(url).content.decode()


def test_recebi_no_detalhe_do_cliente(usuario_logado, cliente):
    html = usuario_logado.get(f"/clientes/{cliente.pk}/").content.decode()
    assert f"{URL}?cliente={cliente.pk}" in html
    assert "✓ Recebi" in html
