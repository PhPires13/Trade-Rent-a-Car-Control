"""Revisão de UX #3 e #6 — WhatsApp certo por origem e vencimento padrão da ND.

#3: o texto acompanha a origem (ND de multas não é "aluguel"), uma mensagem
única por cliente com o total, "Enviar ao motorista" nas NDs e nada de
WhatsApp para cobrança judicial (opção A).
#6: ND sem vencimento vence no PRÓXIMO dia de pagamento do cliente (opção A),
nunca no mesmo dia da emissão.
"""

from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import unquote

import pytest
from django.core.exceptions import ValidationError

from apps.alocacoes.models import Alocacao
from apps.financeiro import services, whatsapp
from apps.financeiro.models import Cobranca
from apps.frota.models import Veiculo
from apps.multas.models import Multa
from apps.pessoas.models import Cliente

QUARTA = Cliente.DiaSemana.QUARTA  # 2


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
    # início quarta-feira 01/07/2026: paga às quartas
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=50_000,
    )


@pytest.fixture(autouse=True)
def pix(settings):
    settings.CHAVE_PIX = "financeiro@traderentacar.com.br"


def cobranca(cliente, origem, descricao, valor, vencimento, status=Cobranca.Status.PENDENTE, **kw):
    return Cobranca.objects.create(
        cliente=cliente,
        origem=origem,
        descricao=descricao,
        valor=Decimal(valor),
        vencimento=vencimento,
        status=status,
        **kw,
    )


# ---------- #6: vencimento padrão da ND ----------


def test_proximo_dia_pagamento_usa_dia_da_alocacao_e_nunca_o_mesmo_dia(alocacao, cliente):
    segunda = date(2026, 10, 19)
    quarta = date(2026, 10, 21)
    assert services.proximo_dia_pagamento(cliente, segunda) == quarta
    # emitida na própria quarta: vai para a quarta seguinte, não para hoje
    assert services.proximo_dia_pagamento(cliente, quarta) == date(2026, 10, 28)
    # quinta → quarta da semana seguinte
    assert services.proximo_dia_pagamento(cliente, date(2026, 10, 22)) == date(2026, 10, 28)


def test_sem_alocacao_ativa_vale_o_dia_do_cadastro(cliente):
    cliente.dia_vencimento = Cliente.DiaSemana.SEXTA
    cliente.save()
    assert services.proximo_dia_pagamento(cliente, date(2026, 10, 20)) == date(2026, 10, 23)


def test_alocacao_ativa_vence_o_dia_do_cadastro(alocacao, cliente):
    cliente.dia_vencimento = Cliente.DiaSemana.SEXTA
    cliente.save()
    assert services.dia_pagamento(cliente) == QUARTA
    assert services.dias_pagamento_por_cliente()[cliente.pk] == QUARTA


def test_sem_dia_de_pagamento_vence_em_7_dias(cliente):
    assert services.proximo_dia_pagamento(cliente, date(2026, 10, 20)) == date(2026, 10, 27)


def test_nd_sem_vencimento_vence_no_proximo_dia_de_pagamento(alocacao, cliente):
    nd = services.emitir_nota_debito(
        cliente, date(2026, 10, 21), [("Avaria para-choque", Decimal("300.00"))]
    )
    assert nd.vencimento == date(2026, 10, 28)
    assert nd.cobranca.vencimento == date(2026, 10, 28)
    # no dia seguinte à emissão continua em dia, sem virar inadimplência
    nd.cobranca.atualizar_status(hoje=date(2026, 10, 22))
    assert nd.cobranca.status == Cobranca.Status.PENDENTE


def test_nd_com_vencimento_antes_da_emissao_e_recusada(cliente):
    with pytest.raises(ValidationError, match="antes da data de emissão"):
        services.emitir_nota_debito(
            cliente,
            date(2026, 10, 21),
            [("Avaria", Decimal("10.00"))],
            vencimento=date(2026, 10, 20),
        )


def test_tela_nd_nova_leva_dias_de_pagamento_e_emite_com_vencimento_sugerido(
    usuario_logado, alocacao, cliente
):
    html = usuario_logado.get("/financeiro/nd/nova/").content.decode()
    assert 'id="nd-dias-pagamento"' in html
    assert f'"{cliente.pk}": {QUARTA}' in html
    assert "próximo dia de pagamento" in html
    hoje = date.today()
    usuario_logado.post(
        "/financeiro/nd/nova/",
        {
            "cliente": cliente.pk,
            "data_emissao": hoje.isoformat(),
            "vencimento": "",
            "item_descricao": ["Franquia"],
            "item_valor": ["150,00"],
            "token": "abc",
        },
    )
    nd = cliente.notas_debito.get()
    assert nd.cobranca.vencimento == services.proximo_dia_pagamento(cliente, hoje)
    assert nd.cobranca.vencimento > hoje


def test_nd_de_multas_vence_no_proximo_dia_de_pagamento(usuario_logado, alocacao, veiculo, cliente):
    multa = Multa.objects.create(
        veiculo=veiculo,
        data_infracao=date(2026, 7, 5),
        descricao="Avançar sinal",
        valor=Decimal("234.78"),
    )
    sugerido = services.proximo_dia_pagamento(cliente, date.today())
    html = usuario_logado.get(f"/multas/gerar-nd/?cliente={cliente.pk}").content.decode()
    assert f'value="{sugerido:%Y-%m-%d}"' in html
    assert "próximo dia de pagamento do cliente" in html
    usuario_logado.post(
        "/multas/gerar-nd/",
        {"cliente": cliente.pk, "multa": [multa.pk], "vencimento": sugerido.isoformat()},
    )
    assert cliente.notas_debito.get().cobranca.vencimento == sugerido


def test_nd_de_multas_com_vencimento_no_passado_nao_emite(
    usuario_logado, alocacao, veiculo, cliente
):
    multa = Multa.objects.create(
        veiculo=veiculo,
        data_infracao=date(2026, 7, 5),
        descricao="Avançar sinal",
        valor=Decimal("234.78"),
    )
    ontem = date.today() - timedelta(days=1)
    resposta = usuario_logado.post(
        "/multas/gerar-nd/",
        {"cliente": cliente.pk, "multa": [multa.pk], "vencimento": ontem.isoformat()},
        follow=True,
    )
    assert not cliente.notas_debito.exists()
    assert "Vencimento inválido" in resposta.content.decode()


# ---------- #3: texto certo por origem ----------


def test_nd_de_multas_nao_fala_em_aluguel(cliente):
    c = cobranca(
        cliente,
        Cobranca.Origem.NOTA_DEBITO,
        "Nota de débito ND 046 (multas)",
        "870.00",
        date(2026, 10, 14),
        Cobranca.Status.ATRASADO,
    )
    texto = whatsapp.mensagem_cobranca(c, hoje=date(2026, 10, 20))
    assert "aluguel" not in texto.lower()
    assert "a nota de débito ND 046 (multas) venceu em 14/10" in texto
    assert "R$ 870,00" in texto
    assert "Pix: financeiro@traderentacar.com.br" in texto


def test_aluguel_cita_o_carro(alocacao, cliente):
    c = cobranca(
        cliente,
        Cobranca.Origem.ALUGUEL,
        "Aluguel semanal QXQ6C10 — venc. 21/10/2026",
        "650.00",
        date(2026, 10, 21),
        alocacao=alocacao,
    )
    texto = whatsapp.mensagem_cobranca(c, hoje=date(2026, 10, 20))
    assert texto.startswith("Olá, Arlen! O aluguel da semana do carro QXQ6C10 vence em 21/10")


@pytest.mark.parametrize(
    ("origem", "descricao", "esperado"),
    [
        (
            Cobranca.Origem.ENCARGO,
            "Encargo por atraso — Aluguel semanal QXQ6C10",
            "o encargo por atraso (Aluguel semanal QXQ6C10)",
        ),
        (
            Cobranca.Origem.EXCEDENTE_KM,
            "Excedente de km QXQ6C10 09/2026 — 120 km além da franquia",
            "o excedente de km (QXQ6C10 09/2026 — 120 km além da franquia)",
        ),
        (
            Cobranca.Origem.REPASSE_MANUTENCAO,
            "Repasse manutenção QXQ6C10 — troca do retrovisor",
            "o repasse de manutenção (QXQ6C10 — troca do retrovisor)",
        ),
        (Cobranca.Origem.OUTRO, "Lavagem", "a cobrança “Lavagem”"),
    ],
)
def test_cada_origem_tem_seu_texto(cliente, origem, descricao, esperado):
    c = cobranca(cliente, origem, descricao, "50.00", date(2026, 10, 15), Cobranca.Status.ATRASADO)
    texto = whatsapp.mensagem_cobranca(c, hoje=date(2026, 10, 20))
    assert f"lembrar que {esperado} venceu em 15/10" in texto
    assert "aluguel que venceu" not in texto


def test_vence_hoje(cliente):
    c = cobranca(cliente, Cobranca.Origem.OUTRO, "Lavagem", "50.00", date(2026, 10, 20))
    assert "vence hoje (20/10)" in whatsapp.mensagem_cobranca(c, hoje=date(2026, 10, 20))


# ---------- #3: judicial sem WhatsApp (opção A) ----------


def test_cobranca_judicial_nao_tem_link(cliente):
    c = cobranca(
        cliente,
        Cobranca.Origem.ALUGUEL,
        "Aluguel semanal",
        "650.00",
        date(2026, 9, 1),
        Cobranca.Status.JUDICIAL,
    )
    assert whatsapp.link_cobranca(c, hoje=date(2026, 10, 20)) == ""


def test_tela_de_cobrancas_esconde_whatsapp_do_judicial(usuario_logado, cliente):
    cobranca(
        cliente,
        Cobranca.Origem.ALUGUEL,
        "Aluguel semanal",
        "650.00",
        date(2026, 9, 1),
        Cobranca.Status.JUDICIAL,
    )
    html = usuario_logado.get("/financeiro/?status=judicial").content.decode()
    assert "wa.me" not in html
    assert "Sem WhatsApp: cobrança judicial" in html


# ---------- #3: mensagem única por cliente ----------


def test_mensagem_do_cliente_lista_itens_e_total_sem_o_judicial(cliente):
    hoje = date(2026, 10, 20)
    itens = [
        cobranca(
            cliente,
            Cobranca.Origem.ALUGUEL,
            "Aluguel semanal",
            "650.00",
            date(2026, 10, 14),
            Cobranca.Status.ATRASADO,
        ),
        cobranca(
            cliente,
            Cobranca.Origem.NOTA_DEBITO,
            "Nota de débito ND 046 (multas)",
            "870.00",
            date(2026, 10, 28),
        ),
        cobranca(
            cliente,
            Cobranca.Origem.OUTRO,
            "Dívida antiga",
            "999.00",
            date(2026, 8, 1),
            Cobranca.Status.JUDICIAL,
        ),
    ]
    texto = whatsapp.mensagem_cliente(cliente, itens, hoje)
    assert "• O aluguel da semana: R$ 650,00 — venceu em 14/10 (6 dias atrás)" in texto
    assert "• A nota de débito ND 046 (multas): R$ 870,00 — vence em 28/10" in texto
    assert "Total: R$ 1.520,00." in texto
    assert "Dívida antiga" not in texto
    link = whatsapp.link_cliente(cliente, itens, hoje)
    assert link.startswith("https://wa.me/5531988887777?text=")
    assert "Total: R$ 1.520,00" in unquote(link)


def test_cliente_com_um_item_recebe_a_mensagem_simples(cliente):
    c = cobranca(cliente, Cobranca.Origem.OUTRO, "Lavagem", "50.00", date(2026, 10, 22))
    hoje = date(2026, 10, 20)
    assert whatsapp.mensagem_cliente(cliente, [c], hoje) == whatsapp.mensagem_cobranca(c, hoje)


def test_tela_de_cobrancas_oferece_cobrar_tudo_uma_vez_por_cliente(usuario_logado, cliente):
    hoje = date.today()
    cobranca(cliente, Cobranca.Origem.OUTRO, "Lavagem", "50.00", hoje + timedelta(days=2))
    cobranca(cliente, Cobranca.Origem.OUTRO, "Combustível", "80.00", hoje + timedelta(days=3))
    html = usuario_logado.get("/financeiro/").content.decode()
    assert html.count("cobrar tudo (2 itens)") == 1
    assert html.count("cobrar no WhatsApp") == 2


def test_extrato_do_cliente_tem_cobrar_no_whatsapp(usuario_logado, cliente):
    cobranca(cliente, Cobranca.Origem.OUTRO, "Lavagem", "50.00", date.today())
    html = usuario_logado.get(f"/financeiro/cliente/{cliente.pk}/").content.decode()
    assert "Cobrar no WhatsApp" in html
    assert "wa.me/5531988887777" in html


# ---------- #3: "Enviar ao motorista" nas NDs ----------


def test_lista_de_nds_tem_enviar_ao_motorista_com_itens_e_pix(usuario_logado, alocacao, cliente):
    hoje = date.today()
    services.emitir_nota_debito(
        cliente,
        hoje,
        [("Avaria para-choque", Decimal("300.00")), ("Franquia", Decimal("150.00"))],
    )
    resposta = usuario_logado.get("/financeiro/nd/")
    html = unquote(resposta.content.decode())
    assert "Enviar ao motorista" in html
    assert "ND 001 da Trade Rent a Car" in html
    assert "• Avaria para-choque: R$ 300,00" in html
    assert "Total: R$ 450,00" in html
    assert "Pix: financeiro@traderentacar.com.br" in html


def test_nd_paga_ou_judicial_nao_tem_enviar(usuario_logado, cliente):
    hoje = date.today()
    paga = services.emitir_nota_debito(cliente, hoje, [("Avaria", Decimal("10.00"))])
    judicial = services.emitir_nota_debito(cliente, hoje, [("Franquia", Decimal("20.00"))])
    Cobranca.objects.filter(pk=paga.cobranca.pk).update(status=Cobranca.Status.PAGO)
    Cobranca.objects.filter(pk=judicial.cobranca.pk).update(status=Cobranca.Status.JUDICIAL)
    html = usuario_logado.get("/financeiro/nd/").content.decode()
    assert "Enviar ao motorista" not in html


def test_mensagem_da_nd_parcial_mostra_o_que_falta(cliente):
    hoje = date(2026, 10, 20)
    nd = services.emitir_nota_debito(cliente, hoje, [("Avaria", Decimal("300.00"))])
    services.registrar_recebimento(
        cliente, hoje, Decimal("100.00"), "pix", [(nd.cobranca, Decimal("100.00"))]
    )
    nd.cobranca.refresh_from_db()
    texto = whatsapp.mensagem_nd(nd, hoje)
    assert "já recebemos R$ 100,00; falta R$ 200,00" in texto


# ---------- revisão adversarial ----------


def test_linha_judicial_nao_ganha_cobrar_tudo(usuario_logado, cliente):
    """Na linha judicial o aviso é 'sem WhatsApp' — o 'cobrar tudo' não pode aparecer ao lado."""
    hoje = date.today()
    cobranca(
        cliente,
        Cobranca.Origem.OUTRO,
        "Dívida antiga",
        "999.00",
        hoje - timedelta(days=60),
        Cobranca.Status.JUDICIAL,
    )
    cobranca(cliente, Cobranca.Origem.OUTRO, "Lavagem", "50.00", hoje + timedelta(days=2))
    cobranca(cliente, Cobranca.Origem.OUTRO, "Combustível", "80.00", hoje + timedelta(days=3))
    html = usuario_logado.get("/financeiro/?status=judicial").content.decode()
    assert "cobrar tudo" not in html
    assert "wa.me" not in html
    # na lista de todas, o "cobrar tudo" vai para a 1ª linha cobrável
    html = usuario_logado.get("/financeiro/?status=todas").content.decode()
    assert html.count("cobrar tudo (2 itens)") == 1


def test_mensagem_ignora_item_sem_saldo(cliente):
    hoje = date(2026, 10, 20)
    aberta = cobranca(cliente, Cobranca.Origem.OUTRO, "Lavagem", "50.00", date(2026, 10, 22))
    quitada = cobranca(cliente, Cobranca.Origem.OUTRO, "Combustível", "80.00", date(2026, 10, 15))
    services.registrar_recebimento(
        cliente, hoje, Decimal("80.00"), "pix", [(quitada, Decimal("80.00"))]
    )
    quitada.refresh_from_db()
    texto = whatsapp.mensagem_cliente(cliente, [aberta, quitada], hoje)
    assert texto == whatsapp.mensagem_cobranca(aberta, hoje)
    assert "Combustível" not in texto


def test_nome_em_branco_nao_derruba_a_mensagem(cliente):
    cliente.nome = "   "
    c = cobranca(cliente, Cobranca.Origem.OUTRO, "Lavagem", "50.00", date(2026, 10, 22))
    assert whatsapp.mensagem_cobranca(c, date(2026, 10, 20)).startswith("Olá, tudo bem!")


def test_nd_de_cliente_sem_telefone_explica_por_que_nao_tem_botao(usuario_logado, cliente):
    Cliente.objects.filter(pk=cliente.pk).update(telefone="")
    services.emitir_nota_debito(cliente, date.today(), [("Avaria", Decimal("10.00"))])
    html = usuario_logado.get("/financeiro/nd/").content.decode()
    assert "Enviar ao motorista" not in html
    assert "Sem telefone no cadastro" in html


def test_lista_de_nds_sem_consulta_por_linha(
    usuario_logado, cliente, django_assert_max_num_queries
):
    hoje = date.today()
    for i in range(6):
        services.emitir_nota_debito(cliente, hoje, [(f"Item {i}", Decimal("10.00"))])
    with django_assert_max_num_queries(12):
        resposta = usuario_logado.get("/financeiro/nd/")
    assert resposta.content.decode().count("Enviar ao motorista") >= 6


def test_tela_de_cobrancas_sem_consulta_por_linha(
    usuario_logado, alocacao, cliente, django_assert_max_num_queries
):
    hoje = date.today()
    for i in range(8):
        cobranca(
            cliente,
            Cobranca.Origem.ALUGUEL,
            f"Aluguel {i}",
            "650.00",
            hoje + timedelta(days=i),
            alocacao=alocacao,
        )
    with django_assert_max_num_queries(14):
        resposta = usuario_logado.get("/financeiro/")
    assert "cobrar tudo (8 itens)" in resposta.content.decode()


@pytest.mark.parametrize(
    "dados",
    [
        {"cliente": "abc", "data_emissao": "2026-10-20"},
        {"cliente": "", "data_emissao": "2026-10-20"},
        {"data_emissao": "2026-10-20", "vencimento": "2026-13-45"},
        {"data_emissao": "ontem"},
    ],
)
def test_nd_nova_com_dado_invalido_nao_da_erro_500(usuario_logado, cliente, dados):
    dados = {"cliente": cliente.pk, **dados, "item_descricao": ["X"], "item_valor": ["10,00"]}
    resposta = usuario_logado.post("/financeiro/nd/nova/", dados)
    assert resposta.status_code == 200
    assert "Não deu para emitir" in resposta.content.decode()
    assert not cliente.notas_debito.exists()


def test_nd_nova_vencimento_antes_da_emissao_mostra_erro(usuario_logado, cliente):
    resposta = usuario_logado.post(
        "/financeiro/nd/nova/",
        {
            "cliente": cliente.pk,
            "data_emissao": "2026-10-21",
            "vencimento": "2026-10-20",
            "item_descricao": ["X"],
            "item_valor": ["10,00"],
        },
    )
    assert resposta.status_code == 200
    assert "não pode ser antes da data de emissão" in resposta.content.decode()
    assert not cliente.notas_debito.exists()
