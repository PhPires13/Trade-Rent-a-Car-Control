from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.alocacoes.models import Alocacao
from apps.financeiro.models import Caucao, Cobranca, MovimentacaoCaucao, NotaDebito
from apps.financeiro.reports import base_das_do_mes, total_a_receber
from apps.financeiro.services import (
    aplicar_encargo,
    atualizar_atrasos_e_inadimplencia,
    distribuir_automatico,
    gerar_cobrancas_semanais,
    registrar_recebimento,
    sugerir_encargo,
)
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


@pytest.fixture
def alocacao(db, cliente):
    veiculo = Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_atual=50_000)
    a = Alocacao(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),  # quarta
        valor_semanal=Decimal("650"),
        dia_vencimento=2,
        km_entrega=50_000,
    )
    a.save()
    return a


def cobranca(cliente, valor, vencimento, origem=Cobranca.Origem.ALUGUEL):
    return Cobranca.objects.create(
        cliente=cliente, origem=origem, valor=Decimal(valor), vencimento=vencimento
    )


# --- Geração de cobranças semanais ---


def test_gera_aluguel_no_dia_de_vencimento(alocacao):
    criadas = gerar_cobrancas_semanais(hoje=date(2026, 7, 8))  # quarta
    assert len(criadas) == 1
    assert criadas[0].valor == Decimal("650")
    assert criadas[0].origem == Cobranca.Origem.ALUGUEL


def test_nao_gera_fora_do_dia(alocacao):
    assert gerar_cobrancas_semanais(hoje=date(2026, 7, 9)) == []  # quinta


def test_nao_duplica_na_mesma_semana(alocacao):
    gerar_cobrancas_semanais(hoje=date(2026, 7, 8))
    criadas = gerar_cobrancas_semanais(hoje=date(2026, 7, 8))
    assert criadas == []
    assert alocacao.cobrancas.filter(origem=Cobranca.Origem.ALUGUEL).count() == 1


def test_valor_ajustado_por_troca(alocacao):
    substituto = Veiculo.objects.create(placa="RUJ3I28", marca_modelo="Voyage")
    alocacao.trocas.create(
        veiculo_substituto=substituto,
        data_retirada=date(2026, 7, 5),
        km_retirada=30_000,
        valor_semanal_ajustado=Decimal("750"),
    )
    substituto.status = Veiculo.Status.ALOCADO
    substituto.save()
    criadas = gerar_cobrancas_semanais(hoje=date(2026, 7, 8))
    assert criadas[0].valor == Decimal("750")


# --- Baixa de recebimento (travas) ---


def test_recebimento_quita_cobranca(cliente):
    c = cobranca(cliente, "650", date(2026, 7, 8))
    _, sobra = registrar_recebimento(cliente, "650", date(2026, 7, 8), "pix", {c.id: "650"})
    c.refresh_from_db()
    assert c.status == Cobranca.Status.PAGO
    assert c.saldo == Decimal("0")
    assert sobra == Decimal("0")


def test_recebimento_parcial(cliente):
    c = cobranca(cliente, "650", date(2026, 7, 8))
    registrar_recebimento(cliente, "400", date(2026, 7, 8), "pix", {c.id: "400"})
    c.refresh_from_db()
    assert c.status == Cobranca.Status.PARCIAL
    assert c.saldo == Decimal("250")


def test_trava_parcela_acima_do_saldo(cliente):
    c = cobranca(cliente, "650", date(2026, 7, 8))
    with pytest.raises(ValidationError, match="excede o saldo"):
        registrar_recebimento(cliente, "700", date(2026, 7, 8), "pix", {c.id: "700"})


def test_trava_total_acima_do_recebido(cliente):
    c1 = cobranca(cliente, "650", date(2026, 7, 8))
    c2 = cobranca(cliente, "650", date(2026, 7, 15))
    with pytest.raises(ValidationError, match="maior que o valor recebido"):
        registrar_recebimento(
            cliente, "650", date(2026, 7, 16), "pix", {c1.id: "650", c2.id: "650"}
        )


def test_sobra_vira_credito(cliente):
    c = cobranca(cliente, "650", date(2026, 7, 8))
    _, sobra = registrar_recebimento(cliente, "700", date(2026, 7, 8), "pix", {c.id: "650"})
    assert sobra == Decimal("50")


def test_distribuir_automatico_mais_antiga_primeiro(cliente):
    c1 = cobranca(cliente, "650", date(2026, 7, 1))
    c2 = cobranca(cliente, "650", date(2026, 7, 8))
    plano = distribuir_automatico([c2, c1], Decimal("800"))
    assert plano[c1.id] == Decimal("650")
    assert plano[c2.id] == Decimal("150")


# --- Atrasos e inadimplência ---


def test_atraso_marca_inadimplente_com_1_dia(cliente):
    cobranca(cliente, "650", date(2026, 7, 8))
    atualizar_atrasos_e_inadimplencia(hoje=date(2026, 7, 9))
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.INADIMPLENTE


def test_quitar_volta_para_ativo(cliente):
    c = cobranca(cliente, "650", date(2026, 7, 8))
    atualizar_atrasos_e_inadimplencia(hoje=date(2026, 7, 9))
    registrar_recebimento(cliente, "650", date(2026, 7, 10), "pix", {c.id: "650"})
    atualizar_atrasos_e_inadimplencia(hoje=date(2026, 7, 11))
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.ATIVO


# --- Encargos por atraso (5%/10%) ---


def test_sugestao_encargo_5_por_cento_ate_4_dias(cliente):
    c = cobranca(cliente, "1000", date(2026, 7, 8))
    proposta = sugerir_encargo(c, hoje=date(2026, 7, 11))  # 3 dias
    assert proposta["percentual"] == Decimal("0.05")
    assert proposta["valor"] == Decimal("50.00")


def test_sugestao_encargo_10_por_cento_acima_de_4_dias(cliente):
    c = cobranca(cliente, "1000", date(2026, 7, 8))
    proposta = sugerir_encargo(c, hoje=date(2026, 7, 20))  # 12 dias
    assert proposta["percentual"] == Decimal("0.10")
    assert proposta["valor"] == Decimal("100.00")


def test_aplicar_encargo_cria_cobranca(cliente):
    c = cobranca(cliente, "1000", date(2026, 7, 8))
    encargo = aplicar_encargo(c, "50", hoje=date(2026, 7, 11))
    assert encargo.origem == Cobranca.Origem.ENCARGO_ATRASO
    assert encargo.valor == Decimal("50.00")
    assert encargo.cobranca_origem == c


def test_encargo_zero_nao_cria(cliente):
    c = cobranca(cliente, "1000", date(2026, 7, 8))
    assert aplicar_encargo(c, "0") is None


# --- Classificação fiscal / DAS ---


def test_das_soma_apenas_aluguel(cliente):
    aluguel = cobranca(cliente, "650", date(2026, 7, 8), Cobranca.Origem.ALUGUEL)
    multa = cobranca(cliente, "234.78", date(2026, 7, 8), Cobranca.Origem.NOTA_DEBITO)
    registrar_recebimento(cliente, "650", date(2026, 7, 10), "pix", {aluguel.id: "650"})
    registrar_recebimento(cliente, "234.78", date(2026, 7, 10), "pix", {multa.id: "234.78"})

    dados = base_das_do_mes(date(2026, 7, 1))
    assert dados["receita_locacao"] == Decimal("650")
    assert dados["pagamentos_diversos"] == Decimal("234.78")
    assert dados["base_das"] == Decimal("650")


def test_encargo_fica_fora_do_das(cliente):
    assert Cobranca.Origem.ENCARGO_ATRASO not in Cobranca.ORIGENS_RECEITA_LOCACAO


def test_total_a_receber_usa_saldo(cliente):
    c = cobranca(cliente, "650", date(2026, 7, 8))
    registrar_recebimento(cliente, "400", date(2026, 7, 8), "pix", {c.id: "400"})
    assert total_a_receber() == Decimal("250")


# --- Nota de débito ---


def test_nota_debito_numeracao_automatica(cliente):
    n1 = NotaDebito.objects.create(cliente=cliente, data_emissao=date(2026, 7, 8))
    n2 = NotaDebito.objects.create(cliente=cliente, data_emissao=date(2026, 7, 9))
    assert n1.numero == 1
    assert n2.numero == 2


def test_nota_debito_total_dos_itens(cliente):
    nota = NotaDebito.objects.create(cliente=cliente, data_emissao=date(2026, 7, 8))
    nota.itens.create(descricao="Multa PBH", valor=Decimal("234.78"))
    nota.itens.create(descricao="Multa DNIT", valor=Decimal("104.13"))
    assert nota.total == Decimal("338.91")


# --- Caução ---


def test_caucao_saldo_reforco_e_desconto(cliente):
    caucao = Caucao.objects.create(cliente=cliente)
    caucao.movimentacoes.create(tipo="reforco", valor=Decimal("1000"), data=date(2026, 7, 1))
    caucao.movimentacoes.create(tipo="desconto", valor=Decimal("300"), data=date(2026, 7, 10))
    assert caucao.saldo == Decimal("700")
    caucao.refresh_from_db()
    assert caucao.status == Caucao.Status.PARCIAL


def test_caucao_nao_devolve_alem_do_saldo(cliente):
    caucao = Caucao.objects.create(cliente=cliente)
    caucao.movimentacoes.create(tipo="reforco", valor=Decimal("500"), data=date(2026, 7, 1))
    mov = MovimentacaoCaucao(
        caucao=caucao, tipo="devolucao", valor=Decimal("600"), data=date(2026, 7, 10)
    )
    with pytest.raises(ValidationError):
        mov.full_clean()


# --- Telas ---


@pytest.fixture
def usuario_logado(client, django_user_model):
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    client.login(username="dono", password="senha-forte-123")
    return client


def test_telas_financeiras_renderizam(usuario_logado, cliente):
    c = cobranca(cliente, "650", date(2026, 7, 8))
    assert usuario_logado.get("/financeiro/cobrancas/").status_code == 200
    assert usuario_logado.get("/financeiro/baixa/").status_code == 200
    assert usuario_logado.get(f"/financeiro/baixa/?cliente={cliente.pk}").status_code == 200
    assert usuario_logado.get(f"/financeiro/cobrancas/{c.pk}/encargo/").status_code == 200
    assert usuario_logado.get("/financeiro/notas/").status_code == 200
    assert usuario_logado.get("/financeiro/caucoes/").status_code == 200
    assert usuario_logado.get("/financeiro/das/").status_code == 200


def test_baixa_pela_tela(usuario_logado, cliente):
    c = cobranca(cliente, "650", date(2026, 7, 8))
    resposta = usuario_logado.post(
        "/financeiro/baixa/",
        {
            "cliente": cliente.pk,
            "valor": "650",
            "data": "2026-07-10",
            "forma": "pix",
            f"parcela_{c.id}": "650",
            "confirmar": "1",
        },
    )
    assert resposta.status_code == 302
    c.refresh_from_db()
    assert c.status == Cobranca.Status.PAGO


def test_exportacao_das_csv(usuario_logado, cliente):
    resposta = usuario_logado.get("/financeiro/das/exportar/?mes=2026-07")
    assert resposta.status_code == 200
    assert resposta["Content-Type"].startswith("text/csv")
    assert "DAS" in resposta.content.decode("utf-8")
