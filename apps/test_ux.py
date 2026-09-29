"""Revisão de UX (etapa 12): formato de dinheiro, entrada brasileira, login e cabeçalho."""

from decimal import Decimal, InvalidOperation

import pytest

from apps.campos import dinheiro_br, exibir_dinheiro
from apps.formatos import brl, milhar


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        (Decimal("14800.00"), "R$ 14.800,00"),
        (Decimal("1234567.5"), "R$ 1.234.567,50"),
        (Decimal("650"), "R$ 650,00"),
        (Decimal("0.5"), "R$ 0,50"),
        (Decimal("-100"), "-R$ 100,00"),
        (None, "—"),
    ],
)
def test_brl(valor, esperado):
    assert brl(valor) == esperado


def test_milhar():
    assert milhar(48900) == "48.900"
    assert milhar(None) == "—"


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("58.000,00", Decimal("58000.00")),
        ("58000,00", Decimal("58000.00")),
        ("58000.00", Decimal("58000.00")),
        ("1.850", Decimal("1850")),
        ("R$ 1.250,5", Decimal("1250.5")),
        ("0.50", Decimal("0.50")),
        ("0,50", Decimal("0.50")),
    ],
)
def test_dinheiro_br_aceita_o_jeito_brasileiro(texto, esperado):
    assert dinheiro_br(texto) == esperado


def test_dinheiro_br_invalido():
    with pytest.raises(InvalidOperation):
        dinheiro_br("abc")


def test_exibir_dinheiro():
    assert exibir_dinheiro(Decimal("58000")) == "58.000,00"


def test_formulario_de_veiculo_aceita_virgula(db):
    from apps.frota.views import VeiculoForm

    form = VeiculoForm(
        {
            "placa": "ZZZ9Z99",
            "marca_modelo": "Gol",
            "uso": "locacao",
            "km_atual": 0,
            "chave_reserva": "duvida",
            "valor_compra": "58.000,00",
        }
    )
    assert form.is_valid(), form.errors
    assert form.cleaned_data["valor_compra"] == Decimal("58000.00")


def test_login_mantem_usuario_apos_erro(client, django_user_model):
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    html = client.post("/entrar/", {"username": "dono", "password": "errada"}).content.decode()
    assert 'value="dono"' in html
    assert "incorretos" in html


def test_cabecalho_destaca_a_secao_atual(usuario_logado):
    html = usuario_logado.get("/frota/").content.decode()
    assert 'aria-current="page"' in html
    assert "Retaguarda (Admin)" in html  # Admin agora dentro do menu da conta


def test_mensagem_de_erro_tem_estilo_de_erro(usuario_logado, db):
    # POST inválido no cancelamento gera messages.error → caixa vermelha com role=alert
    from datetime import date

    from apps.financeiro.models import Cobranca
    from apps.pessoas.models import Cliente

    cliente = Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")
    cobranca = Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.ALUGUEL,
        descricao="Aluguel",
        valor=Decimal("650"),
        vencimento=date(2026, 7, 1),
    )
    html = usuario_logado.post(
        f"/financeiro/cobranca/{cobranca.pk}/cancelar/", follow=True
    ).content.decode()
    assert 'role="alert"' in html
    assert "bg-red-50" in html
