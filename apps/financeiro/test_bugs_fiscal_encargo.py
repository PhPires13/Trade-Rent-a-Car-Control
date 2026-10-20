"""Bugs da frente fiscal-encargo: D13, D31, C16 e o encargo do C04."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.alocacoes.models import Alocacao
from apps.financeiro import services
from apps.financeiro.models import Cobranca
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


@pytest.fixture
def alocacao(cliente):
    # início quarta-feira 01/07/2026; paga às quartas
    veiculo = Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_atual=50_000)
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=50_000,
    )


def _cobranca(cliente, origem=Cobranca.Origem.ALUGUEL, vencimento=date(2026, 7, 1), **extra):
    return Cobranca.objects.create(
        cliente=cliente,
        origem=origem,
        descricao="Aluguel semanal" if origem == Cobranca.Origem.ALUGUEL else "Outra",
        valor=Decimal("650.00"),
        vencimento=vencimento,
        status=Cobranca.Status.ATRASADO,
        **extra,
    )


# ---------- C04 (parte do encargo) ----------


def test_encargo_vence_no_proximo_dia_de_pagamento(alocacao):
    atrasada = _cobranca(alocacao.cliente, alocacao=alocacao)
    # sábado 04/07; o cliente paga às quartas → vence quarta 08/07, não hoje
    encargo = services.aplicar_encargo(atrasada, Decimal("32.50"), hoje=date(2026, 7, 4))
    assert encargo.vencimento == date(2026, 7, 8)


def test_encargo_aplicado_hoje_nao_deixa_inadimplente_amanha(alocacao):
    atrasada = _cobranca(alocacao.cliente, alocacao=alocacao)
    services.aplicar_encargo(atrasada, Decimal("32.50"), hoje=date(2026, 7, 4))
    services.registrar_recebimento(
        alocacao.cliente, date(2026, 7, 4), Decimal("650.00"), "pix", [(atrasada, "650.00")]
    )
    services.marcar_atrasos(hoje=date(2026, 7, 5))
    alocacao.cliente.refresh_from_db()
    assert alocacao.cliente.status == Cliente.Status.ATIVO


def test_nao_aceita_encargo_sobre_encargo(alocacao):
    atrasada = _cobranca(alocacao.cliente, alocacao=alocacao)
    encargo = services.aplicar_encargo(atrasada, Decimal("32.50"), hoje=date(2026, 7, 4))
    encargo.status = Cobranca.Status.ATRASADO
    encargo.save(update_fields=["status"])
    assert services.sugerir_encargo(encargo, hoje=date(2026, 7, 20)) == Decimal("0")
    with pytest.raises(ValidationError):
        services.aplicar_encargo(encargo, Decimal("1.00"), hoje=date(2026, 7, 20))


# ---------- D31: só aluguel, com complemento até 10% ----------


@pytest.mark.parametrize(
    "origem",
    [Cobranca.Origem.NOTA_DEBITO, Cobranca.Origem.EXCEDENTE_KM, Cobranca.Origem.ENCARGO],
)
def test_encargo_so_para_aluguel(cliente, origem):
    cobranca = _cobranca(cliente, origem=origem)
    assert services.sugerir_encargo(cobranca, hoje=date(2026, 7, 4)) == Decimal("0")
    with pytest.raises(ValidationError):
        services.aplicar_encargo(cobranca, Decimal("32.50"), hoje=date(2026, 7, 4))


def test_complemento_de_5_para_10_por_cento_depois_de_4_dias(alocacao):
    atrasada = _cobranca(alocacao.cliente, alocacao=alocacao)
    services.aplicar_encargo(atrasada, Decimal("32.50"), hoje=date(2026, 7, 4))
    # ainda dentro dos 4 dias: nada a sugerir e não deixa aplicar outro
    assert services.sugerir_encargo(atrasada, hoje=date(2026, 7, 5)) == Decimal("0")
    with pytest.raises(ValidationError):
        services.aplicar_encargo(atrasada, Decimal("32.50"), hoje=date(2026, 7, 5))
    # passou de 4 dias: sugere completar até 10% (65,00 − 32,50)
    assert services.sugerir_encargo(atrasada, hoje=date(2026, 7, 10)) == Decimal("32.50")
    complemento = services.aplicar_encargo(atrasada, Decimal("32.50"), hoje=date(2026, 7, 10))
    assert complemento.descricao.startswith("Complemento")
    assert services.sugerir_encargo(atrasada, hoje=date(2026, 7, 12)) == Decimal("0")
    with pytest.raises(ValidationError):  # só um complemento
        services.aplicar_encargo(atrasada, Decimal("5.00"), hoje=date(2026, 7, 12))


def test_tela_de_cobrancas_nao_sugere_encargo_em_nd(usuario_logado, cliente):
    nd = _cobranca(
        cliente, origem=Cobranca.Origem.NOTA_DEBITO, vencimento=date.today() - timedelta(days=3)
    )
    aluguel = _cobranca(cliente, vencimento=date.today() - timedelta(days=3))
    html = usuario_logado.get("/financeiro/").content.decode()
    assert f"cobranca/{aluguel.pk}/encargo/" in html
    assert f"cobranca/{nd.pk}/encargo/" not in html


def test_tela_sugere_complemento_depois_de_5_por_cento(usuario_logado, cliente):
    hoje = date.today()
    aluguel = _cobranca(cliente, vencimento=hoje - timedelta(days=10))
    services.aplicar_encargo(aluguel, Decimal("32.50"), hoje=hoje - timedelta(days=8))
    html = usuario_logado.get("/financeiro/").content.decode()
    assert f"cobranca/{aluguel.pk}/encargo/" in html
    assert 'value="32.50"' in html or 'value="32,50"' in html


# ---------- C16: inadimplência pela data de hoje ----------


def test_pagamento_com_data_antiga_nao_reativa_quem_ainda_deve(alocacao):
    cliente = alocacao.cliente
    hoje = date.today()
    velha = _cobranca(cliente, alocacao=alocacao, vencimento=hoje - timedelta(days=10))
    _cobranca(cliente, alocacao=alocacao, vencimento=hoje - timedelta(days=3))
    services.marcar_atrasos(hoje=hoje)
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.INADIMPLENTE
    # Pix de 6 dias atrás, lançado hoje, quita só a semana mais velha
    services.registrar_recebimento(
        cliente, hoje - timedelta(days=6), Decimal("650.00"), "pix", [(velha, "650.00")]
    )
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.INADIMPLENTE


def test_desconto_de_caucao_com_data_antiga_nao_reativa_quem_ainda_deve(alocacao):
    cliente = alocacao.cliente
    hoje = date.today()
    caucao = services.abrir_caucao(alocacao, Decimal("1000.00"), data=hoje - timedelta(days=20))
    velha = _cobranca(cliente, alocacao=alocacao, vencimento=hoje - timedelta(days=10))
    _cobranca(cliente, alocacao=alocacao, vencimento=hoje - timedelta(days=3))
    services.marcar_atrasos(hoje=hoje)
    services.descontar_da_caucao(caucao, velha, Decimal("650.00"), hoje - timedelta(days=6))
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.INADIMPLENTE


# ---------- D13: adiantamento (crédito do cliente) no DAS ----------


def test_das_mostra_adiantamento_no_mes_do_pix_sem_mudar_a_base(cliente):
    julho = _cobranca(cliente, vencimento=date(2026, 7, 22))
    agosto = _cobranca(cliente, vencimento=date(2026, 8, 5))
    services.registrar_recebimento(
        cliente, date(2026, 7, 28), Decimal("1000.00"), "pix", [(julho, "650.00")]
    )
    resumo = services.resumo_fiscal(2026, 7)
    assert resumo["locacao"] == Decimal("650.00")  # base continua só no uso
    assert resumo["adiantamentos"] == Decimal("350.00")
    assert resumo["entrou_no_caixa"] == Decimal("1000.00")  # bate com o banco

    services.registrar_recebimento(
        cliente, date(2026, 8, 4), Decimal("350.00"), "credito", [(agosto, "350.00")]
    )
    resumo = services.resumo_fiscal(2026, 8)
    assert resumo["locacao"] == Decimal("350.00")  # entra na base quando é usado
    assert resumo["pago_com_credito"] == Decimal("350.00")
    assert resumo["adiantamentos"] == Decimal("0")
    assert resumo["entrou_no_caixa"] == Decimal("0")  # nenhum dinheiro novo em agosto


def test_tela_e_planilha_do_das_mostram_adiantamentos(usuario_logado, cliente):
    julho = _cobranca(cliente, vencimento=date(2026, 7, 22))
    services.registrar_recebimento(
        cliente, date(2026, 7, 28), Decimal("1000.00"), "pix", [(julho, "650.00")]
    )
    html = usuario_logado.get("/financeiro/das/?mes=2026-07").content.decode()
    assert "Adiantamentos recebidos" in html
    assert "350,00" in html
    assert "1.000,00" in html
    csv = usuario_logado.get("/financeiro/das/?mes=2026-07&exportar=csv").content.decode()
    assert "Adiantamentos recebidos" in csv


def test_extrato_mostra_uso_de_credito_como_abatimento(usuario_logado, cliente):
    julho = _cobranca(cliente, vencimento=date(2026, 7, 22))
    agosto = _cobranca(cliente, vencimento=date(2026, 8, 5))
    services.registrar_recebimento(
        cliente, date(2026, 7, 28), Decimal("1000.00"), "pix", [(julho, "650.00")]
    )
    services.registrar_recebimento(
        cliente, date(2026, 8, 4), Decimal("350.00"), "credito", [(agosto, "350.00")]
    )
    html = usuario_logado.get(f"/financeiro/cliente/{cliente.pk}/").content.decode()
    assert "Abatimento com crédito do cliente" in html


# ---------- revisão adversarial ----------


def test_encargo_de_10_por_cento_ja_aplicado_nao_aceita_complemento(alocacao):
    atrasada = _cobranca(alocacao.cliente, alocacao=alocacao)
    # aplicado direto com 10% depois dos 4 dias: não há o que completar
    services.aplicar_encargo(atrasada, Decimal("65.00"), hoje=date(2026, 7, 10))
    assert services.sugerir_encargo(atrasada, hoje=date(2026, 7, 12)) == Decimal("0")
    with pytest.raises(ValidationError):
        services.aplicar_encargo(atrasada, Decimal("65.00"), hoje=date(2026, 7, 12))


def test_tela_nao_sugere_terceiro_encargo(usuario_logado, cliente):
    hoje = date.today()
    aluguel = _cobranca(cliente, vencimento=hoje - timedelta(days=10))
    # dois encargos baixos (o dono ajustou para menos): não sugere um terceiro
    services.aplicar_encargo(aluguel, Decimal("10.00"), hoje=hoje - timedelta(days=8))
    services.aplicar_encargo(aluguel, Decimal("10.00"), hoje=hoje - timedelta(days=2))
    assert services.sugerir_encargo(aluguel, hoje=hoje) == Decimal("0")
    html = usuario_logado.get("/financeiro/").content.decode()
    assert f"cobranca/{aluguel.pk}/encargo/" not in html


def test_encargo_cancelado_libera_novo_encargo(alocacao):
    atrasada = _cobranca(alocacao.cliente, alocacao=alocacao)
    encargo = services.aplicar_encargo(atrasada, Decimal("32.50"), hoje=date(2026, 7, 4))
    encargo.status = Cobranca.Status.CANCELADA
    encargo.save(update_fields=["status"])
    assert services.sugerir_encargo(atrasada, hoje=date(2026, 7, 4)) == Decimal("32.50")
    novo = services.aplicar_encargo(atrasada, Decimal("32.50"), hoje=date(2026, 7, 4))
    assert novo.descricao.startswith("Encargo por atraso")


def test_caixa_nao_conta_sobra_de_credito_mandada_para_a_caucao(alocacao):
    cliente = alocacao.cliente
    services.abrir_caucao(alocacao, Decimal("1000.00"), data=date(2026, 6, 1))
    julho = _cobranca(cliente, alocacao=alocacao, vencimento=date(2026, 7, 22))
    agosto = _cobranca(cliente, alocacao=alocacao, vencimento=date(2026, 8, 5))
    services.registrar_recebimento(
        cliente, date(2026, 7, 28), Decimal("1000.00"), "pix", [(julho, "650.00")]
    )
    # paga 300 com crédito e manda os 50 que sobraram para a caução
    services.registrar_recebimento(
        cliente,
        date(2026, 8, 4),
        Decimal("350.00"),
        "credito",
        [(agosto, "300.00")],
        sobra_destino="caucao",
    )
    resumo = services.resumo_fiscal(2026, 8)
    assert resumo["locacao"] == Decimal("300.00")
    assert resumo["entrou_no_caixa"] == Decimal("0")


def test_das_com_semana_nao_usada_que_vira_credito_continua_batendo(alocacao):
    # Pix aplicado e depois devolvido como crédito (semana não usada): o caixa
    # do mês continua igual ao Pix
    cliente = alocacao.cliente
    semana = _cobranca(cliente, alocacao=alocacao, vencimento=date(2026, 7, 22))
    recebimento = services.registrar_recebimento(
        cliente, date(2026, 7, 20), Decimal("650.00"), "pix", [(semana, "650.00")]
    )
    from apps.financeiro.models import MovimentoCredito

    aplicacao = recebimento.aplicacoes.get()
    alocacao._aplicacao_vira_credito(aplicacao, MovimentoCredito)
    resumo = services.resumo_fiscal(2026, 7)
    assert resumo["locacao"] == Decimal("0")
    assert resumo["adiantamentos"] == Decimal("650.00")
    assert resumo["entrou_no_caixa"] == Decimal("650.00")
