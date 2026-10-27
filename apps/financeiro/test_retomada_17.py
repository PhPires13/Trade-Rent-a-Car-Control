"""Etapa 17 (retomada): revisão adversarial do financeiro — cada correção com teste."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
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

HOJE = timezone.localdate()


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Rogério", cpf_cnpj="111.222.333-55", telefone="31988887777")


@pytest.fixture
def alocacao(cliente):
    veiculo = Veiculo.objects.create(placa="RTA1B17", marca_modelo="Onix")
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=30),
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
        caucao_valor=Decimal("1000.00"),
    )


def _aluguel(cliente, alocacao, dias_atras=10, valor="650.00"):
    cobranca = Cobranca.objects.create(
        cliente=cliente,
        alocacao=alocacao,
        origem=Cobranca.Origem.ALUGUEL,
        descricao=f"Aluguel {dias_atras}",
        valor=Decimal(valor),
        vencimento=HOJE - timedelta(days=dias_atras),
    )
    cobranca.atualizar_status()
    return cobranca


# --- caução: desfazer o desconto de uma cobrança judicial -----------------------


def test_desfazer_desconto_da_caucao_devolve_a_cobranca_a_justica(cliente, alocacao):
    """Desconto que quitou cobrança judicial, desfeito, volta a ser judicial (não "atrasado")."""
    caucao = alocacao.caucao
    services.registrar_movimentacao_caucao(
        caucao, MovimentacaoCaucao.Tipo.RECEBIMENTO, Decimal("1000.00"), HOJE, forma="pix"
    )
    cobranca = _aluguel(cliente, alocacao)
    cobranca.status = Cobranca.Status.JUDICIAL
    cobranca.save(update_fields=["status"])

    desconto = services.descontar_da_caucao(caucao, cobranca, Decimal("650.00"), HOJE)
    cobranca.refresh_from_db()
    assert cobranca.status == Cobranca.Status.PAGO

    services.desfazer_movimentacao_caucao(desconto)
    cobranca.refresh_from_db()
    assert cobranca.status == Cobranca.Status.JUDICIAL
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.INADIMPLENTE


def test_desfazer_desconto_comum_volta_a_atrasado(cliente, alocacao):
    caucao = alocacao.caucao
    services.registrar_movimentacao_caucao(
        caucao, MovimentacaoCaucao.Tipo.RECEBIMENTO, Decimal("1000.00"), HOJE, forma="pix"
    )
    cobranca = _aluguel(cliente, alocacao)
    desconto = services.descontar_da_caucao(caucao, cobranca, Decimal("650.00"), HOJE)
    services.desfazer_movimentacao_caucao(desconto)
    cobranca.refresh_from_db()
    assert cobranca.status == Cobranca.Status.ATRASADO


# --- ND: ano da data de emissão --------------------------------------------------


@pytest.mark.parametrize(
    "emissao",
    [date(26, 10, 20), date(1926, 10, 20), date(HOJE.year + 5, 1, 10)],
)
def test_nd_recusa_ano_absurdo_na_emissao(cliente, emissao):
    with pytest.raises(ValidationError, match="Confira o ano da data de emissão"):
        services.emitir_nota_debito(cliente, emissao, [("Combustível", Decimal("80.00"))])
    assert not cliente.notas_debito.exists()


def test_nd_recusa_ano_absurdo_no_vencimento(cliente):
    with pytest.raises(ValidationError, match="Confira o ano do vencimento"):
        services.emitir_nota_debito(
            cliente, HOJE, [("Combustível", Decimal("80.00"))], vencimento=date(9999, 1, 1)
        )
    assert not cliente.notas_debito.exists()


def test_nd_com_data_normal_continua_emitindo(cliente):
    nd = services.emitir_nota_debito(cliente, HOJE, [("Combustível", Decimal("80.00"))])
    assert nd.total == Decimal("80.00")


def test_tela_da_nd_com_ano_0026_avisa_sem_emitir(usuario_logado, cliente):
    resposta = usuario_logado.post(
        "/financeiro/nd/nova/",
        {
            "cliente": cliente.pk,
            "data_emissao": "0026-10-20",
            "item_descricao": ["Combustível"],
            "item_valor": ["80,00"],
        },
    )
    assert resposta.status_code == 200
    assert "ano" in resposta.content.decode()
    assert not cliente.notas_debito.exists()


# --- Receber: crédito junto + encargo, e o desfazer dos dois -----------------------


def test_receber_com_credito_e_encargo_e_desfazer_volta_tudo(usuario_logado, cliente, alocacao):
    """Pix + crédito guardado + encargo cobrado junto; o desfazer devolve tudo como antes."""
    aluguel = _aluguel(cliente, alocacao, dias_atras=10)
    MovimentoCredito.objects.create(
        cliente=cliente, tipo=MovimentoCredito.Tipo.ENTRADA, valor=Decimal("50.00"), data=HOJE
    )
    resposta = usuario_logado.post(
        "/financeiro/receber/",
        {
            "cliente": cliente.pk,
            "valor": "665,00",
            "data": HOJE.isoformat(),
            "forma": "pix",
            "usar_credito_junto": "1",
            f"aplicar_{aluguel.pk}": "650,00",
            f"incluir_encargo_{aluguel.pk}": "1",
            f"encargo_{aluguel.pk}": "65,00",
            f"aplicar_encargo_{aluguel.pk}": "65,00",
            "sobra_destino": "credito",
        },
    )
    assert resposta.status_code == 302
    aluguel.refresh_from_db()
    encargo = aluguel.encargos.get()
    encargo.refresh_from_db()
    assert aluguel.status == Cobranca.Status.PAGO
    assert encargo.status == Cobranca.Status.PAGO
    assert MovimentoCredito.saldo_do_cliente(cliente) == Decimal("0.00")
    dinheiro = Recebimento.objects.get(cliente=cliente, forma="pix")
    credito = Recebimento.objects.get(cliente=cliente, forma="credito")
    assert dinheiro.valor == Decimal("665.00") and credito.valor == Decimal("50.00")

    resposta = usuario_logado.post(
        f"/financeiro/recebimento/{dinheiro.pk}/desfazer/", {"junto": credito.pk}
    )
    assert resposta.status_code == 302
    assert not Recebimento.objects.filter(cliente=cliente).exists()
    aluguel.refresh_from_db()
    encargo.refresh_from_db()
    assert aluguel.status == Cobranca.Status.ATRASADO
    assert encargo.status == Cobranca.Status.CANCELADA
    assert MovimentoCredito.saldo_do_cliente(cliente) == Decimal("50.00")


def test_receber_com_credito_negativo_nao_quebra(usuario_logado, cliente, alocacao):
    """Saldo de crédito negativo (dado antigo) não vira "crédito a usar negativo"."""
    aluguel = _aluguel(cliente, alocacao)
    MovimentoCredito.objects.create(
        cliente=cliente, tipo=MovimentoCredito.Tipo.USO, valor=Decimal("10.00"), data=HOJE
    )
    resposta = usuario_logado.post(
        "/financeiro/receber/",
        {
            "cliente": cliente.pk,
            "valor": "650,00",
            "data": HOJE.isoformat(),
            "forma": "pix",
            "usar_credito_junto": "1",
            f"aplicar_{aluguel.pk}": "650,00",
        },
    )
    assert resposta.status_code == 302
    aluguel.refresh_from_db()
    assert aluguel.status == Cobranca.Status.PAGO


# --- caução: "desfazer" em duas abas / pela tela --------------------------------


def test_desfazer_desconto_pela_tela_devolve_judicial_e_segundo_clique_avisa(
    usuario_logado, cliente, alocacao
):
    """Botão "↩ desfazer" do desconto: a cobrança volta à Justiça; o 2º clique (outra
    aba) avisa "já foi desfeita" e volta à caução, em vez de "página não encontrada"."""
    caucao = alocacao.caucao
    services.registrar_movimentacao_caucao(
        caucao, MovimentacaoCaucao.Tipo.RECEBIMENTO, Decimal("1000.00"), HOJE, forma="pix"
    )
    cobranca = _aluguel(cliente, alocacao)
    cobranca.status = Cobranca.Status.JUDICIAL
    cobranca.save(update_fields=["status"])
    desconto = services.descontar_da_caucao(caucao, cobranca, Decimal("650.00"), HOJE)
    url = f"/financeiro/caucoes/movimentacao/{desconto.pk}/desfazer/"

    primeira = usuario_logado.post(url)
    assert primeira.status_code == 302
    assert primeira.url == f"/financeiro/caucoes/{caucao.pk}/"
    cobranca.refresh_from_db()
    assert cobranca.status == Cobranca.Status.JUDICIAL

    segunda = usuario_logado.post(url, follow=True)
    assert segunda.status_code == 200
    assert segunda.redirect_chain[-1][0] == f"/financeiro/caucoes/{caucao.pk}/"
    assert "já foi desfeita" in segunda.content.decode()


def test_desfazer_movimentacao_que_nunca_existiu_e_404(usuario_logado):
    assert (
        usuario_logado.post("/financeiro/caucoes/movimentacao/999999/desfazer/").status_code == 404
    )


# --- caução: desconto com data de mês já fechado no DAS ------------------------


@pytest.fixture
def alocacao_antiga(cliente):
    veiculo = Veiculo.objects.create(placa="RTA2C17", marca_modelo="HB20")
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=120),
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
        caucao_valor=Decimal("1000.00"),
    )


def _caucao_com_saldo(alocacao):
    caucao = alocacao.caucao
    services.registrar_movimentacao_caucao(
        caucao,
        MovimentacaoCaucao.Tipo.RECEBIMENTO,
        Decimal("1000.00"),
        alocacao.data_inicio,
        forma="pix",
    )
    return caucao


def test_desconto_de_mes_fechado_pede_confirmacao_como_o_receber(cliente, alocacao_antiga):
    """O desconto entra no DAS do mês da data: mês antes do passado só com confirmação."""
    caucao = _caucao_com_saldo(alocacao_antiga)
    cobranca = _aluguel(cliente, alocacao_antiga, dias_atras=100)
    antiga = services.inicio_do_mes_passado(HOJE) - timedelta(days=1)
    with pytest.raises(ValidationError, match="Confirmo: pagamento de mês passado"):
        services.descontar_da_caucao(
            caucao, cobranca, Decimal("650.00"), antiga, mes_passado_confirmado=False
        )
    assert not caucao.movimentacoes.filter(tipo=MovimentacaoCaucao.Tipo.DESCONTO).exists()
    services.descontar_da_caucao(
        caucao, cobranca, Decimal("650.00"), antiga, mes_passado_confirmado=True
    )
    cobranca.refresh_from_db()
    assert cobranca.status == Cobranca.Status.PAGO


def test_tela_da_caucao_desconto_de_mes_fechado_sem_marcar_nao_lanca(
    usuario_logado, cliente, alocacao_antiga
):
    caucao = _caucao_com_saldo(alocacao_antiga)
    cobranca = _aluguel(cliente, alocacao_antiga, dias_atras=100)
    antiga = services.inicio_do_mes_passado(HOJE) - timedelta(days=1)
    dados = {
        "token": "t1",
        "tipo": "desconto",
        "valor": "650,00",
        "data": antiga.isoformat(),
        "cobranca": cobranca.pk,
    }
    url = f"/financeiro/caucoes/{caucao.pk}/"
    resposta = usuario_logado.post(url, dados)
    assert resposta.status_code == 200
    assert "Confirmo: pagamento de mês passado" in resposta.content.decode()
    assert not caucao.movimentacoes.filter(tipo=MovimentacaoCaucao.Tipo.DESCONTO).exists()

    resposta = usuario_logado.post(url, {**dados, "token": "t2", "confirmar_mes_passado": "1"})
    assert resposta.status_code == 302
    cobranca.refresh_from_db()
    assert cobranca.status == Cobranca.Status.PAGO


def test_tela_da_caucao_desconto_do_mes_passado_nao_pede_confirmacao(
    usuario_logado, cliente, alocacao_antiga
):
    caucao = _caucao_com_saldo(alocacao_antiga)
    cobranca = _aluguel(cliente, alocacao_antiga, dias_atras=40)
    resposta = usuario_logado.post(
        f"/financeiro/caucoes/{caucao.pk}/",
        {
            "token": "t3",
            "tipo": "desconto",
            "valor": "650,00",
            # dia 1º do mês atual: ainda não mexe em DAS fechado (etapa 17, navegador)
            "data": services.inicio_do_mes(HOJE).isoformat(),
            "cobranca": cobranca.pk,
        },
    )
    assert resposta.status_code == 302
