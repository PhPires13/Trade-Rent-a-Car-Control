from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.financeiro import services
from apps.financeiro.models import (
    Caucao,
    Cobranca,
    MovimentoCredito,
    NotaDebito,
)
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_atual=50_000)


@pytest.fixture
def alocacao(veiculo, cliente):
    # início quarta-feira 01/07/2026; vencimento nas quartas
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=50_000,
        caucao_valor=Decimal("1000.00"),
    )


# ---------- geração de cobranças semanais ----------


def test_gera_cobrancas_ate_hoje_e_eh_idempotente(alocacao):
    criadas = services.gerar_cobrancas_semanais(hoje=date(2026, 7, 16))
    assert len(criadas) == 3  # 01/07, 08/07, 15/07
    assert services.gerar_cobrancas_semanais(hoje=date(2026, 7, 16)) == []
    vencimentos = list(
        Cobranca.objects.filter(origem="aluguel").values_list("vencimento", flat=True)
    )
    assert vencimentos == [date(2026, 7, 1), date(2026, 7, 8), date(2026, 7, 15)]


def test_cobranca_usa_valor_da_troca_vigente(alocacao, db):
    substituto = Veiculo.objects.create(placa="RNB9J66", marca_modelo="Voyage")
    TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=date(2026, 7, 6),
        km_retirada=0,
        valor_semanal_ajustado=Decimal("750.00"),
    )
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 9))
    semana2 = Cobranca.objects.get(origem="aluguel", vencimento=date(2026, 7, 8))
    assert semana2.valor == Decimal("750.00")
    semana1 = Cobranca.objects.get(origem="aluguel", vencimento=date(2026, 7, 1))
    assert semana1.valor == Decimal("650.00")


def test_nao_gera_apos_encerramento(alocacao):
    alocacao.encerrar(data_termino=date(2026, 7, 10), km_devolucao=51_000)
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 30))
    assert Cobranca.objects.filter(origem="aluguel").count() == 2  # 01/07 e 08/07


# ---------- atraso, inadimplência e encargos ----------


def test_atraso_marca_cobranca_e_cliente_inadimplente(alocacao, cliente):
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 1))
    services.marcar_atrasos(hoje=date(2026, 7, 2))  # 1 dia de atraso (decisão nº 14)
    cobranca = Cobranca.objects.get(origem="aluguel")
    cliente.refresh_from_db()
    assert cobranca.status == Cobranca.Status.ATRASADO
    assert cliente.status == Cliente.Status.INADIMPLENTE


def test_pagamento_reverte_inadimplencia(alocacao, cliente):
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 1))
    services.marcar_atrasos(hoje=date(2026, 7, 3))
    cobranca = Cobranca.objects.get(origem="aluguel")
    services.registrar_recebimento(
        cliente, date(2026, 7, 3), Decimal("650.00"), "pix", [(cobranca, Decimal("650.00"))]
    )
    services.marcar_atrasos(hoje=date(2026, 7, 3))
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.ATIVO


def test_encargo_sugerido_5_e_10_por_cento(alocacao):
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 1))
    cobranca = Cobranca.objects.get(origem="aluguel")
    # até 4 dias → 5%
    assert services.sugerir_encargo(cobranca, hoje=date(2026, 7, 4)) == Decimal("32.50")
    # acima de 4 dias → 10%
    assert services.sugerir_encargo(cobranca, hoje=date(2026, 7, 10)) == Decimal("65.00")


def test_aplicar_encargo_cria_cobranca_diversa(alocacao):
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 1))
    cobranca = Cobranca.objects.get(origem="aluguel")
    encargo = services.aplicar_encargo(cobranca, Decimal("32.50"), hoje=date(2026, 7, 4))
    assert encargo.origem == Cobranca.Origem.ENCARGO
    assert encargo.classificacao_fiscal == "diverso"  # fora da base do DAS (decisão nº 13)
    with pytest.raises(ValidationError):  # não duplica
        services.aplicar_encargo(cobranca, Decimal("10.00"))


# ---------- recebimento com travas ----------


def test_recebimento_quita_e_gera_sobra_como_credito(alocacao, cliente):
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 1))
    cobranca = Cobranca.objects.get(origem="aluguel")
    services.registrar_recebimento(
        cliente, date(2026, 7, 1), Decimal("700.00"), "pix", [(cobranca, Decimal("650.00"))]
    )
    cobranca.refresh_from_db()
    assert cobranca.status == Cobranca.Status.PAGO
    assert MovimentoCredito.saldo_do_cliente(cliente) == Decimal("50.00")


def test_trava_aplicacao_acima_do_saldo(alocacao, cliente):
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 1))
    cobranca = Cobranca.objects.get(origem="aluguel")
    with pytest.raises(ValidationError):
        services.registrar_recebimento(
            cliente,
            date(2026, 7, 1),
            Decimal("1000.00"),
            "pix",
            [(cobranca, Decimal("700.00"))],
        )


def test_trava_total_acima_do_recebido(alocacao, cliente):
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 9))
    cobrancas = list(Cobranca.objects.filter(origem="aluguel"))
    with pytest.raises(ValidationError):
        services.registrar_recebimento(
            cliente,
            date(2026, 7, 9),
            Decimal("650.00"),
            "pix",
            [(c, Decimal("650.00")) for c in cobrancas],
        )


def test_pagamento_parcial(alocacao, cliente):
    from datetime import timedelta

    hoje = date.today()
    # vencimento no futuro → pagar parte deixa "Parcial"; se já vencida, ficaria "Atrasado"
    cobranca = Cobranca.objects.create(
        cliente=cliente,
        alocacao=alocacao,
        origem=Cobranca.Origem.ALUGUEL,
        descricao="Aluguel semanal",
        valor=Decimal("650.00"),
        vencimento=hoje + timedelta(days=7),
    )
    services.registrar_recebimento(
        cliente, hoje, Decimal("300.00"), "pix", [(cobranca, Decimal("300.00"))]
    )
    cobranca.refresh_from_db()
    assert cobranca.status == Cobranca.Status.PARCIAL
    assert cobranca.saldo == Decimal("350.00")


def test_uso_de_credito_valida_saldo(alocacao, cliente):
    with pytest.raises(ValidationError):
        services.registrar_recebimento(cliente, date(2026, 7, 1), Decimal("100.00"), "credito", [])


# ---------- nota de débito ----------


def test_nd_numeracao_automatica_e_cobranca(cliente):
    nd1 = services.emitir_nota_debito(
        cliente, date(2026, 7, 10), [("Multa avanço de sinal", Decimal("234.78"))]
    )
    nd2 = services.emitir_nota_debito(
        cliente,
        date(2026, 7, 12),
        [("Multa velocidade", Decimal("104.13")), ("Avaria retrovisor", Decimal("80.00"))],
    )
    assert nd2.numero == nd1.numero + 1
    assert nd2.total == Decimal("184.13")
    cobranca = nd2.cobranca
    assert cobranca.valor == Decimal("184.13")
    assert cobranca.classificacao_fiscal == "diverso"


def test_nd_continua_sequencia_existente(cliente):
    NotaDebito.objects.create(numero=97, cliente=cliente, data_emissao=date(2026, 7, 1))
    nd = services.emitir_nota_debito(cliente, date(2026, 7, 10), [("Multa", Decimal("100"))])
    assert nd.numero == 98


# ---------- caução ----------


def test_alocacao_com_caucao_cria_registro(alocacao):
    assert Caucao.objects.filter(alocacao=alocacao).exists()


def test_desconto_da_caucao_quita_cobranca(alocacao, cliente):
    caucao = alocacao.caucao
    services.abrir_caucao(alocacao, valor_recebido=Decimal("1000.00"), data=date(2026, 7, 1))
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 1))
    cobranca = Cobranca.objects.get(origem="aluguel")
    services.descontar_da_caucao(caucao, cobranca, Decimal("650.00"), date(2026, 7, 5))
    cobranca.refresh_from_db()
    assert cobranca.status == Cobranca.Status.PAGO
    assert caucao.saldo == Decimal("350.00")


def test_desconto_nao_pode_exceder_saldo_da_caucao(alocacao, cliente):
    caucao = alocacao.caucao
    services.abrir_caucao(alocacao, valor_recebido=Decimal("100.00"), data=date(2026, 7, 1))
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 1))
    cobranca = Cobranca.objects.get(origem="aluguel")
    with pytest.raises(ValidationError):
        services.descontar_da_caucao(caucao, cobranca, Decimal("650.00"), date(2026, 7, 5))


# ---------- classificação fiscal / base do DAS ----------


def test_base_do_das_so_conta_locacao(alocacao, cliente):
    services.abrir_caucao(alocacao, valor_recebido=Decimal("500.00"), data=date(2026, 7, 1))
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 1))
    aluguel = Cobranca.objects.get(origem="aluguel")
    nd = services.emitir_nota_debito(cliente, date(2026, 7, 5), [("Multa", Decimal("200.00"))])
    services.registrar_recebimento(
        cliente,
        date(2026, 7, 6),
        Decimal("850.00"),
        "pix",
        [(aluguel, Decimal("650.00")), (nd.cobranca, Decimal("200.00"))],
    )
    resumo = services.resumo_fiscal(2026, 7)
    assert resumo["locacao"] == Decimal("650.00")  # só o aluguel entra no DAS
    assert resumo["total_diversos"] == Decimal("200.00")
    assert resumo["caucao_recebida"] == Decimal("500.00")


# ---------- telas ----------


@pytest.fixture
def usuario_logado(client, django_user_model):
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    client.login(username="dono", password="senha-forte-123")
    return client


def test_telas_do_financeiro_renderizam(usuario_logado, alocacao, cliente):
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 1))
    services.emitir_nota_debito(cliente, date(2026, 7, 5), [("Multa", Decimal("100.00"))])
    caucao = alocacao.caucao
    assert usuario_logado.get("/financeiro/").status_code == 200
    assert usuario_logado.get(f"/financeiro/receber/?cliente={cliente.pk}").status_code == 200
    assert usuario_logado.get("/financeiro/nd/").status_code == 200
    assert usuario_logado.get("/financeiro/nd/nova/").status_code == 200
    assert usuario_logado.get("/financeiro/caucoes/").status_code == 200
    assert usuario_logado.get(f"/financeiro/caucoes/{caucao.pk}/").status_code == 200
    assert usuario_logado.get("/financeiro/das/?mes=2026-07").status_code == 200
    assert usuario_logado.get(f"/financeiro/cliente/{cliente.pk}/").status_code == 200
    assert usuario_logado.get("/").status_code == 200  # painel com cards financeiros


def test_receber_pela_tela_com_sobra(usuario_logado, alocacao, cliente):
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 1))
    cobranca = Cobranca.objects.get(origem="aluguel")
    resposta = usuario_logado.post(
        "/financeiro/receber/",
        {
            "cliente": cliente.pk,
            "valor": "700,00",
            "data": "2026-07-01",
            "forma": "pix",
            "sobra_destino": "credito",
            f"aplicar_{cobranca.pk}": "650,00",
        },
    )
    assert resposta.status_code == 302
    cobranca.refresh_from_db()
    assert cobranca.status == Cobranca.Status.PAGO
    assert MovimentoCredito.saldo_do_cliente(cliente) == Decimal("50.00")


def test_exportar_das_csv(usuario_logado, db):
    resposta = usuario_logado.get("/financeiro/das/?mes=2026-07&exportar=csv")
    assert resposta.status_code == 200
    assert resposta["Content-Type"] == "text/csv"
    assert "Receita de loca" in resposta.content.decode()
