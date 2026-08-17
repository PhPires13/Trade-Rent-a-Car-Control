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


def _trocar(alocacao, retirada, valor=Decimal("750.00"), placa="RNB9J66"):
    substituto = Veiculo.objects.create(placa=placa, marca_modelo="Voyage")
    return TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=retirada,
        km_retirada=0,
        valor_semanal_ajustado=valor,
    )


def test_cobranca_rateia_valor_da_troca_por_dias(alocacao, db):
    _trocar(alocacao, retirada=date(2026, 7, 6))  # aberta a partir de segunda 06/07
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 9))
    # semana 08–14/07: troca vigente a semana inteira → valor ajustado cheio
    semana2 = Cobranca.objects.get(origem="aluguel", vencimento=date(2026, 7, 8))
    assert semana2.valor == Decimal("750.00")
    # semana 01–07/07: 5 dias a 650 + 2 dias (06 e 07) a 750 → rateio (docs.md §4.2)
    semana1 = Cobranca.objects.get(origem="aluguel", vencimento=date(2026, 7, 1))
    assert semana1.valor == Decimal("678.57")


def test_troca_que_nao_cruza_o_vencimento_entra_no_rateio(alocacao, db):
    # retirada quinta 02/07 e devolução terça 07/07 — não cruza nenhuma quarta
    troca = _trocar(alocacao, retirada=date(2026, 7, 2))
    troca.devolver(data_devolucao=date(2026, 7, 7), km_devolucao=100)
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 9))
    # semana 01–07/07: 1 dia a 650 + 6 dias (02–07) a 750
    semana1 = Cobranca.objects.get(origem="aluguel", vencimento=date(2026, 7, 1))
    assert semana1.valor == Decimal("735.71")
    # semana 08–14/07: sem troca → valor normal
    semana2 = Cobranca.objects.get(origem="aluguel", vencimento=date(2026, 7, 8))
    assert semana2.valor == Decimal("650.00")


def test_troca_curta_cruzando_o_vencimento_nao_precifica_a_semana_toda(alocacao, db):
    # retirada terça 07/07 e devolução quinta 09/07 — cruza a quarta 08/07
    troca = _trocar(alocacao, retirada=date(2026, 7, 7))
    troca.devolver(data_devolucao=date(2026, 7, 9), km_devolucao=100)
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 9))
    # semana 01–07/07: só o dia 07 a 750
    semana1 = Cobranca.objects.get(origem="aluguel", vencimento=date(2026, 7, 1))
    assert semana1.valor == Decimal("664.29")
    # semana 08–14/07: dias 08 e 09 a 750, o resto a 650 — não os 750 cheios
    semana2 = Cobranca.objects.get(origem="aluguel", vencimento=date(2026, 7, 8))
    assert semana2.valor == Decimal("678.57")


def test_nao_gera_apos_encerramento(alocacao):
    alocacao.encerrar(data_termino=date(2026, 7, 10), km_devolucao=51_000)
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 30))
    assert Cobranca.objects.filter(origem="aluguel").count() == 2  # 01/07 e 08/07


def test_mudar_dia_vencimento_nao_regenera_cobrancas_retroativas(alocacao):
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 16))  # quartas 01, 08 e 15/07
    alocacao.dia_vencimento = 4  # dono muda o vencimento para sexta-feira
    alocacao.save()
    criadas = services.gerar_cobrancas_semanais(hoje=date(2026, 7, 17))
    # a mudança vale só para frente: nada de 03, 10 e 17/07 retroativos
    assert [c.vencimento for c in criadas] == [date(2026, 7, 17)]
    assert Cobranca.objects.filter(origem="aluguel").count() == 4


def test_devolucao_no_dia_do_ciclo_nao_cobra_semana_nao_usada(alocacao):
    # devolução na quarta 15/07 (dia do ciclo): a semana 15–21/07 não é devida
    alocacao.encerrar(data_termino=date(2026, 7, 15), km_devolucao=51_000)
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 16))
    vencimentos = list(
        Cobranca.objects.filter(origem="aluguel").values_list("vencimento", flat=True)
    )
    assert vencimentos == [date(2026, 7, 1), date(2026, 7, 8)]


def test_encerrar_cancela_cobranca_da_semana_nao_usada(alocacao):
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 15))  # cron da manhã já gerou 15/07
    alocacao.encerrar(data_termino=date(2026, 7, 15), km_devolucao=51_000)
    cobranca = Cobranca.objects.get(origem="aluguel", vencimento=date(2026, 7, 15))
    assert cobranca.status == Cobranca.Status.CANCELADA
    # a rotina do dia seguinte não recria nem reativa a semana cancelada
    assert services.gerar_cobrancas_semanais(hoje=date(2026, 7, 16)) == []


def test_encerrar_preserva_cobranca_com_pagamento(alocacao, cliente):
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 15))
    cobranca = Cobranca.objects.get(origem="aluguel", vencimento=date(2026, 7, 15))
    services.registrar_recebimento(
        cliente, date(2026, 7, 15), Decimal("650.00"), "pix", [(cobranca, Decimal("650.00"))]
    )
    alocacao.encerrar(data_termino=date(2026, 7, 15), km_devolucao=51_000)
    cobranca.refresh_from_db()
    assert cobranca.status == Cobranca.Status.PAGO  # paga não é cancelada


# ---------- atraso, inadimplência e encargos ----------


def test_atraso_marca_cobranca_e_cliente_inadimplente(alocacao, cliente):
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 1))
    services.marcar_atrasos(hoje=date(2026, 7, 2))  # 1 dia de atraso (decisão nº 14)
    cobranca = Cobranca.objects.get(origem="aluguel")
    cliente.refresh_from_db()
    assert cobranca.status == Cobranca.Status.ATRASADO
    assert cliente.status == Cliente.Status.INADIMPLENTE


def test_pagamento_reverte_inadimplencia_na_hora(alocacao, cliente):
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 1))
    services.marcar_atrasos(hoje=date(2026, 7, 3))
    cobranca = Cobranca.objects.get(origem="aluguel")
    services.registrar_recebimento(
        cliente, date(2026, 7, 3), Decimal("650.00"), "pix", [(cobranca, Decimal("650.00"))]
    )
    # sem esperar a rotina do dia seguinte (decisão nº 14)
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.ATIVO


def test_pagamento_parcial_nao_reverte_inadimplencia(alocacao, cliente):
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 1))
    services.marcar_atrasos(hoje=date(2026, 7, 3))
    cobranca = Cobranca.objects.get(origem="aluguel")
    services.registrar_recebimento(
        cliente, date(2026, 7, 3), Decimal("300.00"), "pix", [(cobranca, Decimal("300.00"))]
    )
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.INADIMPLENTE


def test_desconto_de_caucao_reverte_inadimplencia_na_hora(alocacao, cliente):
    services.abrir_caucao(alocacao, valor_recebido=Decimal("1000.00"), data=date(2026, 7, 1))
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 1))
    services.marcar_atrasos(hoje=date(2026, 7, 3))
    cobranca = Cobranca.objects.get(origem="aluguel")
    services.descontar_da_caucao(alocacao.caucao, cobranca, Decimal("650.00"), date(2026, 7, 3))
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.ATIVO


def test_cobranca_judicial_mantem_cliente_inadimplente(alocacao, cliente):
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 1))
    services.marcar_atrasos(hoje=date(2026, 7, 10))
    cobranca = Cobranca.objects.get(origem="aluguel")
    cobranca.status = Cobranca.Status.JUDICIAL  # decisão nº 17
    cobranca.save(update_fields=["status"])
    services.marcar_atrasos(hoje=date(2026, 7, 11))  # rotina seguinte não pode reativar
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.INADIMPLENTE


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


def test_base_do_das_inclui_aluguel_quitado_pela_caucao(alocacao, cliente):
    services.abrir_caucao(alocacao, valor_recebido=Decimal("1000.00"), data=date(2026, 7, 1))
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 1))
    aluguel = Cobranca.objects.get(origem="aluguel")
    services.descontar_da_caucao(alocacao.caucao, aluguel, Decimal("650.00"), date(2026, 7, 5))
    nd = services.emitir_nota_debito(cliente, date(2026, 7, 6), [("Multa", Decimal("200.00"))])
    services.descontar_da_caucao(alocacao.caucao, nd.cobranca, Decimal("200.00"), date(2026, 7, 6))
    resumo = services.resumo_fiscal(2026, 7)
    # abater da caução tem o mesmo efeito de quitação (docs.md §4.3): entra na base
    assert resumo["locacao"] == Decimal("650.00")
    assert resumo["total_diversos"] == Decimal("200.00")  # ND via caução fica fora do DAS


# ---------- exclusões no Admin ----------


def test_apagar_recebimento_reabre_cobranca(alocacao, cliente):
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 1))
    cobranca = Cobranca.objects.get(origem="aluguel")
    recebimento = services.registrar_recebimento(
        cliente, date(2026, 7, 1), Decimal("650.00"), "pix", [(cobranca, Decimal("650.00"))]
    )
    cobranca.refresh_from_db()
    assert cobranca.status == Cobranca.Status.PAGO
    recebimento.delete()  # lançado errado, apagado no Admin
    cobranca.refresh_from_db()
    assert cobranca.saldo == Decimal("650.00")
    assert cobranca.status == Cobranca.Status.ATRASADO  # dívida volta a aparecer


def test_apagar_desconto_de_caucao_reabre_cobranca(alocacao, cliente):
    services.abrir_caucao(alocacao, valor_recebido=Decimal("1000.00"), data=date(2026, 7, 1))
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 1))
    cobranca = Cobranca.objects.get(origem="aluguel")
    movimentacao = services.descontar_da_caucao(
        alocacao.caucao, cobranca, Decimal("650.00"), date(2026, 7, 5)
    )
    movimentacao.delete()
    cobranca.refresh_from_db()
    assert cobranca.saldo == Decimal("650.00")
    assert cobranca.status == Cobranca.Status.ATRASADO


def test_sobra_nao_reforca_caucao_de_contrato_encerrado(alocacao, cliente):
    services.abrir_caucao(alocacao, valor_recebido=Decimal("1000.00"), data=date(2026, 7, 1))
    alocacao.encerrar(data_termino=date(2026, 7, 10), km_devolucao=51_000)
    with pytest.raises(ValidationError):
        services.registrar_recebimento(
            cliente, date(2026, 7, 12), Decimal("100.00"), "pix", [], sobra_destino="caucao"
        )


# ---------- telas ----------


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
