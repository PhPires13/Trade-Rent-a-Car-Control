"""Revisão de UX do financeiro (auditoria grupo B).

Cobre: judicial com volta e recebível, recebimento que preserva o digitado,
fonte única dos conjuntos de status, paginação de cobranças, ND idempotente,
?mes= inválido e telas de lista com queries fixas.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.alocacoes.models import Alocacao
from apps.financeiro import services
from apps.financeiro import views as financeiro_views
from apps.financeiro.models import (
    AplicacaoRecebimento,
    Cobranca,
    NotaDebito,
    Recebimento,
)
from apps.frota.models import Veiculo
from apps.pessoas import views as pessoas_views
from apps.pessoas.models import Cliente


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


def cobranca_de(
    cliente,
    valor="650.00",
    status=Cobranca.Status.PENDENTE,
    vencimento=None,
    origem=Cobranca.Origem.ALUGUEL,
    descricao="Aluguel semanal QXQ6C10",
):
    return Cobranca.objects.create(
        cliente=cliente,
        origem=origem,
        descricao=descricao,
        valor=Decimal(valor),
        vencimento=vencimento or date(2026, 7, 1),
        status=status,
    )


# ---------- judicial com confirmação, volta e recebimento ----------


def test_marcar_judicial_pede_confirmacao(usuario_logado, cliente):
    # atrasada de hoje: sem encargo sugerido, o botão "judicial" aparece
    cobranca_de(cliente, status=Cobranca.Status.ATRASADO, vencimento=date.today())
    html = usuario_logado.get("/financeiro/").content.decode()
    assert "Marcar como cobrança judicial?" in html  # confirm() no form


def test_reabrir_aparece_para_cobranca_judicial(usuario_logado, cliente):
    cobranca_de(cliente, status=Cobranca.Status.JUDICIAL)
    html = usuario_logado.get("/financeiro/?status=judicial").content.decode()
    assert "reabrir" in html


def test_reabrir_judicial_recalcula_status(usuario_logado, cliente):
    cobranca = cobranca_de(cliente, status=Cobranca.Status.JUDICIAL, vencimento=date(2026, 7, 1))
    resposta = usuario_logado.post(f"/financeiro/cobranca/{cobranca.pk}/reabrir/")
    assert resposta.status_code == 302
    cobranca.refresh_from_db()
    assert cobranca.status == Cobranca.Status.ATRASADO  # vencida volta como atrasada


def test_reabrir_so_atua_em_cobranca_judicial(usuario_logado, cliente):
    cobranca = cobranca_de(cliente, status=Cobranca.Status.PENDENTE)
    usuario_logado.post(f"/financeiro/cobranca/{cobranca.pk}/reabrir/")
    cobranca.refresh_from_db()
    assert cobranca.status == Cobranca.Status.PENDENTE


def test_judicial_aparece_na_tela_de_receber(usuario_logado, cliente):
    cobranca_de(cliente, status=Cobranca.Status.JUDICIAL, descricao="Dívida em acordo")
    html = usuario_logado.get(f"/financeiro/receber/?cliente={cliente.pk}").content.decode()
    assert "Dívida em acordo" in html
    assert "Judicial" in html


def test_receber_quita_cobranca_judicial(usuario_logado, cliente):
    cobranca = cobranca_de(cliente, status=Cobranca.Status.JUDICIAL)
    resposta = usuario_logado.post(
        "/financeiro/receber/",
        {
            "cliente": cliente.pk,
            "valor": "650,00",
            "data": "2026-07-20",
            "forma": "pix",
            "sobra_destino": "credito",
            f"aplicar_{cobranca.pk}": "650,00",
        },
    )
    assert resposta.status_code == 302
    cobranca.refresh_from_db()
    assert cobranca.status == Cobranca.Status.PAGO


def test_judicial_parcialmente_paga_continua_judicial(usuario_logado, cliente):
    cobranca = cobranca_de(cliente, status=Cobranca.Status.JUDICIAL)
    usuario_logado.post(
        "/financeiro/receber/",
        {
            "cliente": cliente.pk,
            "valor": "300,00",
            "data": "2026-07-20",
            "forma": "pix",
            "sobra_destino": "credito",
            f"aplicar_{cobranca.pk}": "300,00",
        },
    )
    cobranca.refresh_from_db()
    assert cobranca.status == Cobranca.Status.JUDICIAL
    assert cobranca.saldo == Decimal("350.00")


def test_judicial_pode_ser_descontada_da_caucao(usuario_logado, cliente, db):
    veiculo = Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol")
    alocacao = Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
        caucao_valor=Decimal("1000.00"),
    )
    services.abrir_caucao(alocacao, valor_recebido=Decimal("1000.00"), data=date(2026, 7, 1))
    cobranca = cobranca_de(cliente, status=Cobranca.Status.JUDICIAL, descricao="Dívida em acordo")
    caucao = alocacao.caucao
    html = usuario_logado.get(f"/financeiro/caucoes/{caucao.pk}/").content.decode()
    assert "Dívida em acordo" in html  # judicial listada para desconto
    resposta = usuario_logado.post(
        f"/financeiro/caucoes/{caucao.pk}/",
        {"tipo": "desconto", "valor": "650,00", "data": "2026-07-20", "cobranca": cobranca.pk},
    )
    assert resposta.status_code == 302
    cobranca.refresh_from_db()
    assert cobranca.status == Cobranca.Status.PAGO


# ---------- fonte única dos conjuntos de status ----------


def test_fonte_unica_dos_conjuntos_de_status():
    assert Cobranca.STATUS_DEVIDOS == [*Cobranca.STATUS_EM_ABERTO, Cobranca.Status.JUDICIAL]
    assert financeiro_views.ABERTAS is Cobranca.STATUS_EM_ABERTO  # alias compatível
    assert pessoas_views.COBRANCAS_EM_ABERTO is Cobranca.STATUS_DEVIDOS


# ---------- erro no recebimento preserva o digitado ----------


def test_erro_no_recebimento_preserva_o_digitado(usuario_logado, cliente):
    c1 = cobranca_de(cliente, valor="650.00", descricao="Aluguel semana 1")
    c2 = cobranca_de(
        cliente, valor="180.00", descricao="Aluguel semana 2", vencimento=date(2026, 7, 8)
    )
    resposta = usuario_logado.post(
        "/financeiro/receber/",
        {
            "cliente": cliente.pk,
            "valor": "900,00",
            "data": "2026-07-20",
            "forma": "dinheiro",
            "sobra_destino": "caucao",
            "observacoes": "pagamento na loja",
            f"aplicar_{c1.pk}": "650,00",
            f"aplicar_{c2.pk}": "200,00",  # acima do saldo de 180 → erro
        },
    )
    assert resposta.status_code == 200
    html = resposta.content.decode()
    assert 'value="900,00"' in html
    assert 'value="650,00"' in html
    assert 'value="200,00"' in html
    assert 'value="pagamento na loja"' in html
    assert 'value="dinheiro" selected' in html
    assert 'value="caucao" selected' in html
    mensagens = [str(m) for m in resposta.context["messages"]]
    assert any("acima do saldo devedor" in m for m in mensagens)
    assert not Recebimento.objects.exists()  # nada foi lançado


def test_valor_com_ponto_de_milhar_aceito(usuario_logado, cliente):
    cobranca = cobranca_de(cliente, valor="1300.00")
    resposta = usuario_logado.post(
        "/financeiro/receber/",
        {
            "cliente": cliente.pk,
            "valor": "1.300,00",
            "data": "2026-07-20",
            "forma": "pix",
            "sobra_destino": "credito",
            f"aplicar_{cobranca.pk}": "1.300,00",
        },
    )
    assert resposta.status_code == 302
    cobranca.refresh_from_db()
    assert cobranca.status == Cobranca.Status.PAGO


def test_mensagem_diz_qual_campo_falhou(usuario_logado, cliente):
    cobranca_de(cliente)
    resposta = usuario_logado.post(
        "/financeiro/receber/",
        {
            "cliente": cliente.pk,
            "valor": "setecentos",
            "data": "2026-07-20",
            "forma": "pix",
            "sobra_destino": "credito",
        },
    )
    mensagens = [str(m) for m in resposta.context["messages"]]
    assert any("Valor recebido inválido" in m for m in mensagens)


# ---------- paginação da tela de cobranças ----------


def _cobrancas_em_lote(cliente, quantidade=120):
    Cobranca.objects.bulk_create(
        [
            Cobranca(
                cliente=cliente,
                origem=Cobranca.Origem.OUTRO,
                descricao=f"Avulsa {i}",
                valor=Decimal("100.00"),
                vencimento=date(2026, 1, 1) + timedelta(days=i),
            )
            for i in range(quantidade)
        ]
    )


def test_cobrancas_todas_paginadas_com_recentes_primeiro(usuario_logado, cliente):
    _cobrancas_em_lote(cliente)
    resposta = usuario_logado.get("/financeiro/", {"status": "todas"})
    pagina = resposta.context["pagina"]
    assert pagina.paginator.count == 120
    assert len(resposta.context["linhas"]) == 50
    primeira, _ = resposta.context["linhas"][0]
    assert primeira.vencimento == date(2026, 1, 1) + timedelta(days=119)
    html = resposta.content.decode()
    assert "de 120 cobranças" in html  # nada de corte silencioso
    assert "pagina=2" in html


def test_cobrancas_abertas_mantem_mais_antiga_primeiro(usuario_logado, cliente):
    _cobrancas_em_lote(cliente)
    resposta = usuario_logado.get("/financeiro/", {"status": "abertas"})
    primeira, _ = resposta.context["linhas"][0]
    assert primeira.vencimento == date(2026, 1, 1)  # cobrar o atraso mais velho primeiro


def test_cobrancas_pagina_2(usuario_logado, cliente):
    _cobrancas_em_lote(cliente)
    resposta = usuario_logado.get("/financeiro/", {"status": "todas", "pagina": "3"})
    assert len(resposta.context["linhas"]) == 20  # 120 = 50 + 50 + 20


# ---------- ND: clique duplo e erro sem perder itens ----------


def test_nd_clique_duplo_nao_duplica(usuario_logado, cliente):
    dados = {
        "cliente": cliente.pk,
        "data_emissao": "2026-07-10",
        "token": "tok-unico-123",
        "item_descricao": ["Multa avanço de sinal"],
        "item_valor": ["234,78"],
    }
    assert usuario_logado.post("/financeiro/nd/nova/", dados).status_code == 302
    resposta = usuario_logado.post("/financeiro/nd/nova/", dados, follow=True)
    assert NotaDebito.objects.count() == 1  # segunda submissão ignorada
    mensagens = [str(m) for m in resposta.context["messages"]]
    assert any("clique duplo" in m for m in mensagens)


def test_nd_erro_preserva_itens_digitados(usuario_logado, cliente):
    resposta = usuario_logado.post(
        "/financeiro/nd/nova/",
        {
            "cliente": cliente.pk,
            "data_emissao": "2026-07-10",
            "token": "tok-2",
            "item_descricao": ["Multa avanço de sinal", "Avaria no retrovisor"],
            "item_valor": ["abc", "80,00"],
        },
    )
    assert resposta.status_code == 200
    html = resposta.content.decode()
    assert "Multa avan" in html  # itens digitados voltam preenchidos
    assert "Avaria no retrovisor" in html
    assert not NotaDebito.objects.exists()


# ---------- ?mes= inválido não derruba a tela do DAS ----------


@pytest.mark.parametrize("mes", ["2026-13", "2026-00", "13", "banana"])
def test_das_mes_invalido_cai_no_mes_atual(usuario_logado, db, mes):
    assert usuario_logado.get(f"/financeiro/das/?mes={mes}").status_code == 200


# ---------- telas de lista com queries fixas ----------


def test_cobrancas_roda_com_queries_fixas(usuario_logado, cliente, django_assert_max_num_queries):
    _cobrancas_em_lote(cliente, quantidade=60)
    with django_assert_max_num_queries(8):
        assert usuario_logado.get("/financeiro/?status=todas").status_code == 200


def test_caucoes_roda_com_queries_fixas(usuario_logado, db, django_assert_max_num_queries):
    for i in range(3):
        cliente = Cliente.objects.create(nome=f"Cliente {i}", cpf_cnpj=f"000.000.00{i}-00")
        veiculo = Veiculo.objects.create(placa=f"QXQ6C1{i}", marca_modelo="Gol")
        alocacao = Alocacao.objects.create(
            veiculo=veiculo,
            cliente=cliente,
            data_inicio=date(2026, 7, 1),
            valor_semanal=Decimal("650.00"),
            km_entrega=0,
            caucao_valor=Decimal("1000.00"),
        )
        services.abrir_caucao(alocacao, valor_recebido=Decimal("1000.00"), data=date(2026, 7, 1))
    with django_assert_max_num_queries(6):
        assert usuario_logado.get("/financeiro/caucoes/").status_code == 200


def test_total_quitado_anotado_bate_com_o_calculado(cliente, db):
    veiculo = Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol")
    alocacao = Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
        caucao_valor=Decimal("500.00"),
    )
    services.abrir_caucao(alocacao, valor_recebido=Decimal("500.00"), data=date(2026, 7, 1))
    cobranca = cobranca_de(cliente)
    services.registrar_recebimento(
        cliente, date(2026, 7, 2), Decimal("100.00"), "pix", [(cobranca, Decimal("100.00"))]
    )
    services.descontar_da_caucao(alocacao.caucao, cobranca, Decimal("50.00"), date(2026, 7, 3))
    anotada = Cobranca.com_quitacao_anotada(Cobranca.objects.filter(pk=cobranca.pk)).get()
    assert anotada.total_quitado == Decimal("150.00")
    assert anotada.saldo == Decimal("500.00")
    cobranca.refresh_from_db()
    assert anotada.total_quitado == cobranca.total_quitado  # anotado = calculado


def test_atualizar_status_nao_confia_em_anotacao_velha(cliente):
    cobranca = cobranca_de(cliente, valor="100.00")
    anotada = Cobranca.com_quitacao_anotada(Cobranca.objects.filter(pk=cobranca.pk)).get()
    recebimento = Recebimento.objects.create(
        cliente=cliente, data=date(2026, 7, 20), valor=Decimal("100.00")
    )
    AplicacaoRecebimento.objects.create(
        recebimento=recebimento, cobranca=anotada, valor=Decimal("100.00")
    )
    anotada.atualizar_status()  # anotação de antes do pagamento não pode valer
    assert anotada.status == Cobranca.Status.PAGO


# ---------- distribuir automaticamente na tela de receber ----------


def test_botao_distribuir_traz_o_saldo_de_cada_cobranca(usuario_logado, db):
    """A distribuição roda no navegador: cada linha precisa levar o saldo."""
    from apps.pessoas.models import Cliente

    cliente = Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")
    Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.ALUGUEL,
        descricao="Aluguel semanal",
        valor=Decimal("650.00"),
        vencimento=date(2026, 7, 1),
    )
    html = usuario_logado.get(f"/financeiro/receber/?cliente={cliente.pk}").content.decode()
    assert "distribuir automaticamente" in html
    assert 'data-saldo="650.00"' in html
