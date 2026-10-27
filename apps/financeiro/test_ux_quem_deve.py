"""Proposta #8 (opção C) — bloco "Hoje" do Painel e tela "Quem deve"."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.financeiro import devedores
from apps.financeiro.models import AplicacaoRecebimento, Cobranca, Recebimento
from apps.pessoas.models import Cliente

HOJE = timezone.localdate()


def _cliente(nome, cpf, telefone="(31) 98888-7777"):
    return Cliente.objects.create(nome=nome, cpf_cnpj=cpf, telefone=telefone)


def _cobranca(cliente, dias, valor="650.00", status=None):
    """Cobrança vencendo `dias` dias atrás (0 = hoje)."""
    vencimento = HOJE - timedelta(days=dias)
    if status is None:
        status = Cobranca.Status.ATRASADO if dias > 0 else Cobranca.Status.PENDENTE
    return Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.ALUGUEL,
        descricao="Aluguel semanal",
        valor=Decimal(valor),
        vencimento=vencimento,
        status=status,
    )


def _pagar(cobranca, valor=None):
    valor = Decimal(valor) if valor else cobranca.valor
    recebimento = Recebimento.objects.create(cliente=cobranca.cliente, data=HOJE, valor=valor)
    AplicacaoRecebimento.objects.create(recebimento=recebimento, cobranca=cobranca, valor=valor)


@pytest.mark.django_db
def test_agrupa_por_cliente_com_total_desde_quando_e_qtde():
    arlen = _cliente("Arlen Souza", "111.222.333-44")
    _cobranca(arlen, 9)
    _cobranca(arlen, 2)
    _cobranca(arlen, 0)
    parcial = _cobranca(arlen, 16)
    _pagar(parcial, "600.00")  # falta R$ 50

    [devedor] = devedores.devedores(HOJE)
    assert devedor.cliente == arlen
    assert devedor.atrasado == Decimal("1350.00")
    assert devedor.vence_hoje == Decimal("650.00")
    assert devedor.total == Decimal("2000.00")
    assert devedor.mais_antigo == HOJE - timedelta(days=16)
    assert devedor.dias_atraso == 16
    assert len(devedor.cobrancas) == 4
    assert devedor.link_whatsapp.startswith("https://wa.me/5531988887777")


@pytest.mark.django_db
def test_ordem_atraso_mais_antigo_primeiro_e_ignora_futuro_pago_e_cancelado():
    so_hoje = _cliente("Bruno", "222.333.444-55")
    _cobranca(so_hoje, 0)
    antigo = _cliente("Carla", "333.444.555-66")
    _cobranca(antigo, 20)
    recente = _cliente("Davi", "444.555.666-77")
    _cobranca(recente, 3)
    fora = _cliente("Eva", "555.666.777-88")
    _cobranca(fora, -5)  # vence semana que vem: não está devendo
    _cobranca(fora, 4, status=Cobranca.Status.CANCELADA)
    quitada = _cobranca(fora, 8, status=Cobranca.Status.PAGO)
    _pagar(quitada)

    nomes = [d.cliente.nome for d in devedores.devedores(HOJE)]
    assert nomes == ["Carla", "Davi", "Bruno"]


@pytest.mark.django_db
def test_judicial_entra_na_conta_mas_sem_whatsapp():
    cliente = _cliente("Fábio", "666.777.888-99")
    _cobranca(cliente, 40, status=Cobranca.Status.JUDICIAL)
    [devedor] = devedores.devedores(HOJE)
    assert devedor.judicial
    assert devedor.atrasado == Decimal("650.00")
    assert devedor.link_whatsapp == ""


@pytest.mark.django_db
def test_painel_mostra_bloco_hoje_com_ate_5_linhas_e_botoes(usuario_logado):
    clientes = [_cliente(f"Cliente {i}", f"000.000.000-0{i}") for i in range(7)]
    for i, cliente in enumerate(clientes):
        _cobranca(cliente, i + 1)
    pago = _cliente("Gil Pagou", "777.888.999-00")
    _pagar(_cobranca(pago, 0, status=Cobranca.Status.PAGO))

    resposta = usuario_logado.get("/")
    contexto = resposta.context
    assert len(contexto["hoje_linhas"]) == 5
    assert contexto["hoje_mais"] == 2
    assert contexto["hoje_qtde"] == 7
    assert contexto["hoje_total"] == Decimal("4550.00")
    assert contexto["hoje_pagos"] == ["Gil Pagou"]
    html = resposta.content.decode()
    assert "Hoje:" in html
    assert "Cliente 6" in html and "Cliente 0" not in html  # mais antigo primeiro
    assert "atrasado há 7 dias" in html
    assert f"{reverse('financeiro:receber')}?cliente={clientes[6].pk}" in html
    assert "https://wa.me/5531988887777" in html
    assert "e mais 2 clientes" in html
    assert reverse("financeiro:quem_deve") in html
    assert "Já pagaram o que vencia hoje: Gil Pagou" in html


@pytest.mark.django_db
def test_painel_sem_devedores_diz_que_esta_tudo_em_dia(usuario_logado):
    html = usuario_logado.get("/").content.decode()
    assert "Ninguém devendo nem vencendo hoje" in html


@pytest.mark.django_db
def test_tela_quem_deve_lista_por_cliente_com_resumo(usuario_logado):
    arlen = _cliente("Arlen Souza", "111.222.333-44")
    _cobranca(arlen, 9)
    _cobranca(arlen, 2)
    bruno = _cliente("Bruno Lima", "222.333.444-55", telefone="")
    _cobranca(bruno, 0)

    resposta = usuario_logado.get(reverse("financeiro:quem_deve"))
    assert resposta.status_code == 200
    assert [d.cliente for d in resposta.context["devedores"]] == [arlen, bruno]
    assert resposta.context["total_geral"] == Decimal("1950.00")
    html = resposta.content.decode()
    assert "R$ 1.950,00" in html
    assert "2 clientes" in html
    assert "atrasado há 9 dias" in html
    assert "vence hoje" in html
    assert "(31) 98888-7777" in html
    assert 'href="tel:31988887777"' in html  # só dígitos: disca em qualquer celular
    assert f"?cliente={bruno.pk}" in html


@pytest.mark.django_db
def test_tela_quem_deve_vazia(usuario_logado):
    html = usuario_logado.get(reverse("financeiro:quem_deve")).content.decode()
    assert "Ninguém devendo nem vencendo hoje" in html


@pytest.mark.django_db
def test_consultas_nao_crescem_com_os_devedores(usuario_logado):
    def contar():
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            assert usuario_logado.get(reverse("financeiro:quem_deve")).status_code == 200
        return len(ctx.captured_queries)

    um = _cliente("Um", "100.000.000-01")
    _cobranca(um, 3)
    poucos = contar()
    for i in range(8):
        cliente = _cliente(f"Outro {i}", f"200.000.000-0{i}")
        _cobranca(cliente, i + 1)
        _cobranca(cliente, 0)
    assert contar() == poucos


# --- revisão adversarial -------------------------------------------------


@pytest.mark.django_db
def test_status_desatualizado_e_parcial_de_hoje_entram_pelo_saldo():
    """Vale o vencimento e o saldo, não o status gravado (o job de atraso pode não ter rodado)."""
    cliente = _cliente("Gabi", "123.123.123-12")
    _cobranca(cliente, 3, status=Cobranca.Status.PENDENTE)  # venceu, status velho
    hoje = _cobranca(cliente, 0, status=Cobranca.Status.PARCIAL)
    _pagar(hoje, "200.00")
    [devedor] = devedores.devedores(HOJE)
    assert devedor.atrasado == Decimal("650.00")
    assert devedor.vence_hoje == Decimal("450.00")
    assert devedor.dias_atraso == 3


@pytest.mark.django_db
def test_quitada_a_mais_nao_entra_nem_deixa_total_negativo():
    cliente = _cliente("Hugo", "234.234.234-23")
    a_mais = _cobranca(cliente, 2)
    _pagar(a_mais, "700.00")  # status ainda "atrasado", mas saldo negativo
    assert devedores.devedores(HOJE) == []
    assert devedores.resumo_do_dia(HOJE)["hoje_total"] == Decimal("0")


@pytest.mark.django_db
def test_judicial_misturada_soma_no_total_mas_fica_fora_da_mensagem():
    from urllib.parse import unquote

    cliente = _cliente("Iara", "345.345.345-34")
    _cobranca(cliente, 30, valor="1000.00", status=Cobranca.Status.JUDICIAL)
    _cobranca(cliente, 2)
    [devedor] = devedores.devedores(HOJE)
    assert devedor.total == Decimal("1650.00")
    assert devedor.judicial
    assert devedor.dias_atraso == 30
    mensagem = unquote(devedor.link_whatsapp)
    assert "650,00" in mensagem and "1.000,00" not in mensagem


@pytest.mark.django_db
def test_pagou_hoje_mas_deve_outra_fica_so_na_lista_de_devedores():
    cliente = _cliente("João", "456.456.456-45")
    _pagar(_cobranca(cliente, 0, status=Cobranca.Status.PAGO))
    _cobranca(cliente, 5)
    contexto = devedores.resumo_do_dia(HOJE)
    assert [d.cliente for d in contexto["hoje_linhas"]] == [cliente]
    assert contexto["hoje_pagos"] == []


@pytest.mark.django_db
def test_painel_judicial_sem_whatsapp_recebi_ocupa_a_linha(usuario_logado):
    cliente = _cliente("Kátia", "567.567.567-56")
    _cobranca(cliente, 10, status=Cobranca.Status.JUDICIAL)
    html = usuario_logado.get("/").content.decode()
    assert "na Justiça" in html
    assert "col-span-2 flex min-h-[48px]" in html
    assert "wa.me/5531988887777" not in html


@pytest.mark.django_db
def test_painel_consultas_nao_crescem_com_os_devedores(usuario_logado):
    def contar():
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            assert usuario_logado.get("/").status_code == 200
        return len(ctx.captured_queries)

    _cobranca(_cliente("Um", "100.000.000-01"), 3)
    poucos = contar()
    for i in range(8):
        cliente = _cliente(f"Outro {i}", f"200.000.000-0{i}")
        _cobranca(cliente, i + 1)
        _pagar(_cobranca(cliente, 0, status=Cobranca.Status.PAGO))
    assert contar() == poucos


def test_quem_deve_exige_login(client):
    resposta = client.get(reverse("financeiro:quem_deve"))
    assert resposta.status_code == 302
    assert "/entrar/" in resposta["Location"]
