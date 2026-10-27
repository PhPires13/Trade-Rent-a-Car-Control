"""Revisão da tela de receber (propostas de UX nº 1-A e nº 5-A).

Casos de borda achados na revisão: toque duplo no Desfazer, parâmetros de
URL estranhos, desfazer só a 2ª parte de uma cobrança, crédito usado e
devolvido no mesmo lançamento, recibo de pagamento com crédito e login.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.messages import get_messages
from django.utils import timezone

from apps.financeiro import recibo, services
from apps.financeiro.models import Cobranca, MovimentoCredito, Recebimento
from apps.pessoas.models import Cliente

URL = "/financeiro/receber/"


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(
        nome="Maria Souza", cpf_cnpj="555.666.777-88", telefone="(11) 97777-6666"
    )


@pytest.fixture
def atrasada(cliente):
    cobranca = Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.ALUGUEL,
        descricao="Aluguel semanal ABC1D23",
        valor=Decimal("650.00"),
        vencimento=timezone.localdate() - timedelta(days=10),
        status=Cobranca.Status.ATRASADO,
    )
    services.atualizar_inadimplencia(cliente)
    return cobranca


def mensagens(resposta):
    return [str(m) for m in get_messages(resposta.wsgi_request)]


def desfazer(client, recebimento_id):
    return client.post(f"/financeiro/recebimento/{recebimento_id}/desfazer/")


def test_toque_duplo_no_desfazer_avisa_em_vez_de_pagina_nao_encontrada(
    usuario_logado, cliente, atrasada
):
    recebimento = services.registrar_recebimento(
        cliente, timezone.localdate(), Decimal("650.00"), "pix", [(atrasada, Decimal("650.00"))]
    )
    pk = recebimento.pk
    assert desfazer(usuario_logado, pk).status_code == 302
    segunda = desfazer(usuario_logado, pk)
    assert segunda.status_code == 302
    assert segunda.url == f"{URL}?cliente={cliente.pk}"
    assert any("já foi desfeito" in m for m in mensagens(segunda))
    atrasada.refresh_from_db()
    assert atrasada.saldo == Decimal("650.00")  # devolvido uma vez só


def test_desfazer_de_lancamento_que_nunca_existiu_e_404(usuario_logado, db):
    assert desfazer(usuario_logado, 999999).status_code == 404


def test_servico_recusa_desfazer_duas_vezes(cliente, atrasada):
    recebimento = services.registrar_recebimento(
        cliente, timezone.localdate(), Decimal("650.00"), "pix", [(atrasada, Decimal("650.00"))]
    )
    mesmo = Recebimento.objects.get(pk=recebimento.pk)  # a outra aba já tinha carregado
    services.desfazer_recebimento(recebimento)
    with pytest.raises(services.ValidationError, match="já foi desfeito"):
        services.desfazer_recebimento(mesmo)


def test_desfazer_exige_login(client, cliente, atrasada):
    recebimento = services.registrar_recebimento(
        cliente, timezone.localdate(), Decimal("650.00"), "pix", [(atrasada, Decimal("650.00"))]
    )
    resposta = desfazer(client, recebimento.pk)
    assert resposta.status_code == 302
    assert "/login" in resposta.url or "/entrar" in resposta.url or "next=" in resposta.url
    assert Recebimento.objects.filter(pk=recebimento.pk).exists()


@pytest.mark.parametrize("param", ["cobranca", "recibo"])
@pytest.mark.parametrize("estranho", ["²", "１２", "9" * 30, "-1", "abc"])
def test_parametros_estranhos_na_url_nao_dao_erro(
    usuario_logado, cliente, atrasada, param, estranho
):
    resposta = usuario_logado.get(URL, {"cliente": cliente.pk, param: estranho})
    assert resposta.status_code == 200


@pytest.mark.parametrize("estranho", ["²", "9" * 30])
def test_cliente_estranho_na_url_e_404_e_nao_500(usuario_logado, db, estranho):
    assert usuario_logado.get(URL, {"cliente": estranho}).status_code == 404


def test_desfazer_so_a_segunda_parte_volta_a_parcial_com_o_saldo_certo(cliente):
    futura = Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.ALUGUEL,
        descricao="Aluguel da semana que vem",
        valor=Decimal("650.00"),
        vencimento=timezone.localdate() + timedelta(days=5),
    )
    services.registrar_recebimento(
        cliente, timezone.localdate(), Decimal("300.00"), "pix", [(futura, Decimal("300.00"))]
    )
    segundo = services.registrar_recebimento(
        cliente, timezone.localdate(), Decimal("350.00"), "dinheiro", [(futura, Decimal("350.00"))]
    )
    futura.refresh_from_db()
    assert futura.status == Cobranca.Status.PAGO
    services.desfazer_recebimento(segundo)
    futura.refresh_from_db()
    assert futura.status == Cobranca.Status.PARCIAL
    assert futura.saldo == Decimal("350.00")


def test_desfazer_judicial_abatido_em_parte_continua_judicial(cliente):
    judicial = Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.ALUGUEL,
        descricao="Acordo judicial",
        valor=Decimal("1000.00"),
        vencimento=timezone.localdate() - timedelta(days=60),
        status=Cobranca.Status.JUDICIAL,
    )
    recebimento = services.registrar_recebimento(
        cliente, timezone.localdate(), Decimal("200.00"), "pix", [(judicial, Decimal("200.00"))]
    )
    services.desfazer_recebimento(recebimento)
    judicial.refresh_from_db()
    assert judicial.status == Cobranca.Status.JUDICIAL
    assert judicial.saldo == Decimal("1000.00")


def test_desfaz_uso_de_credito_que_devolveu_sobra_ao_credito(cliente, atrasada):
    """Usou R$ 700 de crédito, só R$ 650 nas cobranças: R$ 50 voltaram como crédito.

    Depois a sobra foi gasta — desfazer ainda é seguro (devolve os R$ 700 usados),
    então não pode ser recusado com "o crédito já foi usado".
    """
    MovimentoCredito.objects.create(
        cliente=cliente, tipo="entrada", valor=Decimal("700.00"), data=timezone.localdate()
    )
    recebimento = services.registrar_recebimento(
        cliente, timezone.localdate(), Decimal("700.00"), "credito", [(atrasada, Decimal("650.00"))]
    )
    MovimentoCredito.objects.create(
        cliente=cliente, tipo="uso", valor=Decimal("50.00"), data=timezone.localdate()
    )
    assert MovimentoCredito.saldo_do_cliente(cliente) == Decimal("0.00")
    services.desfazer_recebimento(recebimento)
    assert MovimentoCredito.saldo_do_cliente(cliente) == Decimal("650.00")
    atrasada.refresh_from_db()
    assert atrasada.saldo == Decimal("650.00")


def test_recibo_de_pagamento_com_credito_nao_diz_que_recebeu_dinheiro(cliente, atrasada):
    MovimentoCredito.objects.create(
        cliente=cliente, tipo="entrada", valor=Decimal("650.00"), data=timezone.localdate()
    )
    recebimento = services.registrar_recebimento(
        cliente, timezone.localdate(), Decimal("650.00"), "credito", [(atrasada, Decimal("650.00"))]
    )
    texto = recibo.mensagem_recibo(services.resumo_recebimento(recebimento))
    assert "Usamos R$ 650,00 do seu crédito" in texto
    assert "Recebemos seu pagamento" not in texto


def test_resumo_de_pagamento_com_credito_diz_credito_usado(usuario_logado, cliente, atrasada):
    MovimentoCredito.objects.create(
        cliente=cliente, tipo="entrada", valor=Decimal("650.00"), data=timezone.localdate()
    )
    recebimento = services.registrar_recebimento(
        cliente, timezone.localdate(), Decimal("650.00"), "credito", [(atrasada, Decimal("650.00"))]
    )
    html = usuario_logado.get(URL, {"cliente": cliente.pk, "recibo": recebimento.pk})
    assert "Crédito usado: R$ 650,00" in html.content.decode()


def test_sem_divida_nao_oferece_usar_o_credito(usuario_logado, cliente):
    MovimentoCredito.objects.create(
        cliente=cliente, tipo="entrada", valor=Decimal("80.00"), data=timezone.localdate()
    )
    html = usuario_logado.get(URL, {"cliente": cliente.pk}).content.decode()
    assert "Usar o crédito" not in html
    assert "R$ 80,00" in html  # o saldo de crédito continua à vista


def test_toque_duplo_mostra_o_resumo_do_primeiro_lancamento(usuario_logado, cliente, atrasada):
    dados = {
        "cliente": cliente.pk,
        "valor": "650,00",
        "data": timezone.localdate().isoformat(),
        "forma": "pix",
        "sobra_destino": "credito",
        f"aplicar_{atrasada.pk}": "650,00",
    }
    usuario_logado.post(URL, dados)
    primeiro = Recebimento.objects.get()
    segunda = usuario_logado.post(URL, dados)
    assert segunda.url == f"{URL}?cliente={cliente.pk}&recibo={primeiro.pk}"
    assert Recebimento.objects.count() == 1
    assert MovimentoCredito.saldo_do_cliente(cliente) == Decimal("0.00")


def test_botao_desfazer_trava_no_envio(usuario_logado, cliente, atrasada):
    recebimento = services.registrar_recebimento(
        cliente, timezone.localdate(), Decimal("650.00"), "pix", [(atrasada, Decimal("650.00"))]
    )
    html = usuario_logado.get(URL, {"cliente": cliente.pk, "recibo": recebimento.pk})
    assert "this.querySelector('button').disabled = true" in html.content.decode()
