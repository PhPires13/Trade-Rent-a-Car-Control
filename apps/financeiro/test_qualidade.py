"""Qualidade do financeiro: telas com queries que não crescem por linha e travas de entrada.

Cada teste de performance mede a mesma tela com poucas e com muitas linhas:
o número de consultas tem de ser o mesmo (nada de N+1 por cobrança/ND/movimento).
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.alocacoes.models import Alocacao
from apps.financeiro import services
from apps.financeiro.models import (
    Cobranca,
    MovimentacaoCaucao,
    MovimentoCredito,
)
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44", telefone="31987654321")


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


def _queries(client, url):
    with CaptureQueriesContext(connection) as contexto:
        assert client.get(url).status_code == 200
    return len(contexto.captured_queries)


def _cobrancas_pagas_em_parte(cliente, quantidade, inicio=0, status=Cobranca.Status.ATRASADO):
    """Cobranças vencidas com um pagamento parcial cada (saldo e encargo por linha)."""
    for i in range(inicio, inicio + quantidade):
        cobranca = Cobranca.objects.create(
            cliente=cliente,
            origem=Cobranca.Origem.ALUGUEL,
            descricao=f"Aluguel semanal {i}",
            valor=Decimal("650.00"),
            vencimento=date(2026, 7, 1) + timedelta(days=i),
            status=status,
        )
        services.registrar_recebimento(
            cliente, date(2026, 7, 2), Decimal("100.00"), "pix", [(cobranca, Decimal("100.00"))]
        )


# ---------- telas sem N+1 ----------


def test_cobrancas_atrasadas_nao_consultam_por_linha(usuario_logado, cliente):
    _cobrancas_pagas_em_parte(cliente, 2)
    poucas = _queries(usuario_logado, "/financeiro/")
    _cobrancas_pagas_em_parte(cliente, 8, inicio=2)
    assert _queries(usuario_logado, "/financeiro/") == poucas


def test_extrato_do_cliente_nao_consulta_por_linha(usuario_logado, cliente):
    _cobrancas_pagas_em_parte(cliente, 2)
    url = f"/financeiro/cliente/{cliente.pk}/"
    poucas = _queries(usuario_logado, url)
    _cobrancas_pagas_em_parte(cliente, 8, inicio=2)
    assert _queries(usuario_logado, url) == poucas


def test_receber_nao_consulta_por_linha(usuario_logado, cliente):
    _cobrancas_pagas_em_parte(cliente, 2)
    url = f"/financeiro/receber/?cliente={cliente.pk}"
    poucas = _queries(usuario_logado, url)
    _cobrancas_pagas_em_parte(cliente, 8, inicio=2)
    assert _queries(usuario_logado, url) == poucas


def test_nds_nao_consultam_por_nota(usuario_logado, cliente):
    def emitir(quantidade):
        for _ in range(quantidade):
            services.emitir_nota_debito(
                cliente, date(2026, 7, 10), [("Multa", Decimal("100.00")), ("Avaria", "50.00")]
            )

    emitir(2)
    poucas = _queries(usuario_logado, "/financeiro/nd/")
    emitir(8)
    assert _queries(usuario_logado, "/financeiro/nd/") == poucas


def test_detalhe_da_caucao_nao_consulta_por_movimento(usuario_logado, cliente, alocacao):
    caucao = services.abrir_caucao(alocacao, Decimal("1000.00"), date(2026, 7, 1))
    _cobrancas_pagas_em_parte(cliente, 2)
    url = f"/financeiro/caucoes/{caucao.pk}/"
    poucas = _queries(usuario_logado, url)
    _cobrancas_pagas_em_parte(cliente, 8, inicio=2)
    for cobranca in Cobranca.objects.all()[:5]:
        services.descontar_da_caucao(caucao, cobranca, Decimal("10.00"), date(2026, 7, 3))
    assert _queries(usuario_logado, url) == poucas


def test_detalhe_da_caucao_mostra_saldos(usuario_logado, cliente, alocacao):
    caucao = services.abrir_caucao(alocacao, Decimal("1000.00"), date(2026, 7, 1))
    _cobrancas_pagas_em_parte(cliente, 1)
    cobranca = Cobranca.objects.get()
    services.descontar_da_caucao(caucao, cobranca, Decimal("200.00"), date(2026, 7, 3))
    html = usuario_logado.get(f"/financeiro/caucoes/{caucao.pk}/").content.decode()
    assert "R$ 800,00" in html  # saldo guardado
    assert "R$ 350,00" in html  # saldo da cobrança: 650 − 100 pago − 200 da caução


def test_marcar_atrasos_nao_regrava_status_igual(cliente):
    cobranca = Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.ALUGUEL,
        descricao="Aluguel",
        valor=Decimal("650.00"),
        vencimento=date(2026, 7, 1),
    )
    services.marcar_atrasos(hoje=date(2026, 7, 5))
    cobranca.refresh_from_db()
    assert cobranca.status == Cobranca.Status.ATRASADO
    historico = cobranca.history.count()
    services.marcar_atrasos(hoje=date(2026, 7, 6))  # nada mudou: sem nova versão no histórico
    assert cobranca.history.count() == historico


def test_saldo_de_credito_numa_consulta(cliente, django_assert_num_queries):
    MovimentoCredito.objects.create(
        cliente=cliente, tipo="entrada", valor=Decimal("80.00"), data=date(2026, 7, 1)
    )
    MovimentoCredito.objects.create(
        cliente=cliente, tipo="uso", valor=Decimal("30.00"), data=date(2026, 7, 2)
    )
    with django_assert_num_queries(1):
        assert MovimentoCredito.saldo_do_cliente(cliente) == Decimal("50.00")


def test_saldo_de_credito_sem_movimento_eh_zero(cliente):
    assert MovimentoCredito.saldo_do_cliente(cliente) == Decimal("0.00")


# ---------- travas de entrada ----------


@pytest.mark.parametrize("tipo", ["recebimento", "reforco", "devolucao"])
def test_caucao_recusa_valor_negativo(usuario_logado, alocacao, tipo):
    caucao = services.abrir_caucao(alocacao, Decimal("1000.00"), date(2026, 7, 1))
    resposta = usuario_logado.post(
        f"/financeiro/caucoes/{caucao.pk}/",
        {"tipo": tipo, "valor": "-500,00", "data": "2026-07-10", "forma": "pix"},
    )
    assert resposta.status_code == 200
    assert "maior que zero" in resposta.content.decode()
    assert caucao.movimentacoes.count() == 1  # só o depósito inicial


def test_caucao_recusa_tipo_desconhecido(usuario_logado, alocacao):
    caucao = services.abrir_caucao(alocacao, Decimal("1000.00"), date(2026, 7, 1))
    resposta = usuario_logado.post(
        f"/financeiro/caucoes/{caucao.pk}/",
        {"tipo": "saque", "valor": "100,00", "data": "2026-07-10"},
    )
    assert resposta.status_code == 200
    assert caucao.movimentacoes.count() == 1


def test_desconto_de_caucao_recusa_valor_negativo(cliente, alocacao):
    from django.core.exceptions import ValidationError

    caucao = services.abrir_caucao(alocacao, Decimal("1000.00"), date(2026, 7, 1))
    _cobrancas_pagas_em_parte(cliente, 1)
    cobranca = Cobranca.objects.get()
    with pytest.raises(ValidationError):
        services.descontar_da_caucao(caucao, cobranca, Decimal("-100.00"), date(2026, 7, 3))
    assert not MovimentacaoCaucao.objects.filter(tipo="desconto").exists()


@pytest.mark.parametrize("status", [Cobranca.Status.PAGO, Cobranca.Status.CANCELADA])
def test_nao_manda_para_judicial_cobranca_encerrada(usuario_logado, cliente, status):
    cobranca = Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.ALUGUEL,
        descricao="Aluguel",
        valor=Decimal("650.00"),
        vencimento=date(2026, 7, 1),
        status=status,
    )
    usuario_logado.post(f"/financeiro/cobranca/{cobranca.pk}/judicial/")
    cobranca.refresh_from_db()
    assert cobranca.status == status


def test_receber_com_cliente_invalido_da_404(usuario_logado, db):
    assert usuario_logado.get("/financeiro/receber/?cliente=abc").status_code == 404


def test_erro_no_recebimento_mostra_situacao_real(usuario_logado, cliente):
    """1ª aplicação válida + 2ª acima do saldo: tudo desfeito e a tela volta com o banco."""
    _cobrancas_pagas_em_parte(cliente, 2)
    primeira, segunda = Cobranca.objects.order_by("vencimento")
    resposta = usuario_logado.post(
        "/financeiro/receber/",
        {
            "cliente": cliente.pk,
            "valor": "2000,00",
            "data": "2026-07-20",
            "forma": "pix",
            f"aplicar_{primeira.pk}": "550,00",
            f"aplicar_{segunda.pk}": "999,00",
        },
    )
    assert resposta.status_code == 200
    linhas = dict(resposta.context["linhas"])
    assert {c.pk: c.status for c in linhas} == {
        primeira.pk: Cobranca.Status.ATRASADO,
        segunda.pk: Cobranca.Status.ATRASADO,
    }
    assert all(c.saldo == Decimal("550.00") for c in linhas)
