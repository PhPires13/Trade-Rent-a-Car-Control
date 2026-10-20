"""Bugs da geração do aluguel semanal (frente geracao-aluguel: D01, D02, D04, D12)."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.financeiro import services
from apps.financeiro.models import Cobranca
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_atual=50_000)


def _alocar(veiculo, cliente, inicio=date(2026, 7, 1), **extra):
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=inicio,
        valor_semanal=Decimal("650.00"),
        km_entrega=50_000,
        **extra,
    )


@pytest.fixture
def alocacao(veiculo, cliente):
    # início quarta-feira 01/07/2026; vencimento nas quartas
    return _alocar(veiculo, cliente)


def _alugueis():
    return list(Cobranca.objects.filter(origem="aluguel").order_by("vencimento"))


def _sem_buraco_nem_dobra(cobrancas, inicio):
    """Os períodos das cobranças emendam dia a dia, a partir de `inicio`."""
    esperado = inicio
    for cobranca in cobrancas:
        de, ate = services.periodo_da_cobranca(cobranca)
        assert de == esperado, cobranca.descricao
        esperado = ate + timedelta(days=1)


# ---------- D01: contrato antigo na carga inicial ----------


def test_d01_cobrar_a_partir_de_nao_gera_semanas_antigas(veiculo, cliente):
    # contrato no papel desde terça 03/03/2026; no sistema, cobra a partir de 20/10
    alocacao = _alocar(
        veiculo, cliente, inicio=date(2026, 3, 3), cobrar_a_partir_de=date(2026, 10, 20)
    )
    services.gerar_cobrancas_semanais(hoje=date(2026, 10, 20))
    services.marcar_atrasos(hoje=date(2026, 10, 20))
    cobrancas = _alugueis()
    assert [(c.vencimento, c.valor) for c in cobrancas] == [(date(2026, 10, 20), Decimal("650.00"))]
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.ATIVO
    assert alocacao.dia_vencimento == 1  # terça, dia do início


def test_d01_cobrar_a_partir_de_fora_do_dia_sai_proporcional(veiculo, cliente):
    # vence às terças; acertado por fora até domingo 18/10 → cobra 19/10 (seg) só 1 dia
    _alocar(veiculo, cliente, inicio=date(2026, 3, 3), cobrar_a_partir_de=date(2026, 10, 19))
    services.gerar_cobrancas_semanais(hoje=date(2026, 10, 20))
    cobrancas = _alugueis()
    assert [(c.vencimento, c.valor) for c in cobrancas] == [
        (date(2026, 10, 19), Decimal("92.86")),
        (date(2026, 10, 20), Decimal("650.00")),
    ]
    _sem_buraco_nem_dobra(cobrancas, date(2026, 10, 19))


def test_d01_cobrar_a_partir_de_antes_do_inicio_e_recusado(veiculo, cliente):
    from django.core.exceptions import ValidationError

    alocacao = Alocacao(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        cobrar_a_partir_de=date(2026, 6, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=50_000,
    )
    with pytest.raises(ValidationError) as erro:
        alocacao.full_clean()
    assert "cobrar_a_partir_de" in erro.value.message_dict


# ---------- D12: dia de pagar diferente do dia da entrega ----------


def test_d12_dias_ate_o_primeiro_vencimento_sao_cobrados(veiculo, cliente):
    # entrega quarta 01/07, paga às segundas → 01 a 05/07 (5 dias) no dia da entrega
    _alocar(veiculo, cliente, dia_vencimento=0)
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 13))
    cobrancas = _alugueis()
    assert [(c.vencimento, c.valor) for c in cobrancas] == [
        (date(2026, 7, 1), Decimal("464.29")),  # 650 / 7 × 5
        (date(2026, 7, 6), Decimal("650.00")),
        (date(2026, 7, 13), Decimal("650.00")),
    ]
    assert "5 dias" in cobrancas[0].descricao
    _sem_buraco_nem_dobra(cobrancas, date(2026, 7, 1))


def test_d12_entrega_no_dia_de_pagar_continua_semana_cheia(alocacao):
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 1))
    (cobranca,) = _alugueis()
    assert cobranca.valor == Decimal("650.00")
    assert services.periodo_da_cobranca(cobranca) == (date(2026, 7, 1), date(2026, 7, 7))
    assert cobranca.descricao.startswith("Aluguel semanal")


# ---------- D04: mudança do dia de vencimento em contrato rodando ----------


def test_d04_quarta_para_sexta_cobra_so_os_dias_nao_cobertos(alocacao):
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 16))  # quartas 01, 08 e 15/07
    alocacao.dia_vencimento = 4  # sexta
    alocacao.save()
    (transicao,) = services.gerar_cobrancas_semanais(hoje=date(2026, 7, 17))
    # 15/07 já paga até 21/07; a de sexta 17/07 cobre só 22 e 23/07
    assert transicao.vencimento == date(2026, 7, 17)
    assert transicao.valor == Decimal("185.71")
    assert services.periodo_da_cobranca(transicao) == (date(2026, 7, 22), date(2026, 7, 23))
    (cheia,) = services.gerar_cobrancas_semanais(hoje=date(2026, 7, 24))
    assert cheia.valor == Decimal("650.00")
    _sem_buraco_nem_dobra(_alugueis(), date(2026, 7, 1))


def test_d04_quarta_para_terca_nao_deixa_dias_sem_cobrar(alocacao):
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 16))
    alocacao.dia_vencimento = 1  # terça
    alocacao.save()
    (transicao,) = services.gerar_cobrancas_semanais(hoje=date(2026, 7, 21))
    # 22 a 27/07 = 6 dias
    assert transicao.vencimento == date(2026, 7, 21)
    assert transicao.valor == Decimal("557.14")
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 28))
    _sem_buraco_nem_dobra(_alugueis(), date(2026, 7, 1))


def test_d04_mudanca_logo_na_primeira_semana_proporcional(veiculo, cliente):
    # entrega quarta 01/07 com pagamento na segunda; dono corrige para sexta em 02/07
    alocacao = _alocar(veiculo, cliente, dia_vencimento=0)
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 1))  # 01 a 05/07
    alocacao.dia_vencimento = 4
    alocacao.save()
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 10))
    cobrancas = _alugueis()
    vencimentos = [c.vencimento for c in cobrancas]
    assert vencimentos == [date(2026, 7, 1), date(2026, 7, 3), date(2026, 7, 10)]
    # 1ª já cobriu até 05/07; a de sexta 03/07 cobre 06 a 09/07 (4 dias)
    assert cobrancas[1].valor == Decimal("371.43")
    assert cobrancas[2].valor == Decimal("650.00")
    _sem_buraco_nem_dobra(cobrancas, date(2026, 7, 1))


# ---------- D02: carro substituto com outro preço no meio da semana ----------


def _trocar(alocacao, retirada, valor=Decimal("750.00")):
    substituto = Veiculo.objects.create(placa="RNB9J66", marca_modelo="Voyage")
    return TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=retirada,
        km_retirada=0,
        valor_semanal_ajustado=valor,
    )


def test_d02_troca_no_meio_da_semana_ja_lancada_reajusta(alocacao):
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 8))  # semana 08–14/07 = 650
    _trocar(alocacao, date(2026, 7, 10))  # 08–09 Gol, 10–14 Voyage
    semana = Cobranca.objects.get(vencimento=date(2026, 7, 8))
    assert semana.valor == Decimal("721.43")  # (2×650 + 5×750) / 7
    semana_anterior = Cobranca.objects.get(vencimento=date(2026, 7, 1))
    assert semana_anterior.valor == Decimal("650.00")


def test_d02_devolucao_no_meio_da_semana_reajusta(alocacao):
    troca = _trocar(alocacao, date(2026, 7, 10))
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 15))
    semana = Cobranca.objects.get(vencimento=date(2026, 7, 15))
    assert semana.valor == Decimal("750.00")  # troca aberta na geração
    troca.devolver(data_devolucao=date(2026, 7, 17), km_devolucao=100)
    semana.refresh_from_db()
    assert semana.valor == Decimal("692.86")  # 15–17 Voyage, 18–21 Gol
    assert Cobranca.objects.get(vencimento=date(2026, 7, 8)).valor == Decimal("721.43")


def test_d02_semana_paga_nao_muda(alocacao, cliente):
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 8))
    semana = Cobranca.objects.get(vencimento=date(2026, 7, 8))
    services.registrar_recebimento(
        cliente,
        date(2026, 7, 8),
        Decimal("650.00"),
        "pix",
        [(semana, Decimal("650.00"))],
    )
    _trocar(alocacao, date(2026, 7, 10))
    semana.refresh_from_db()
    assert semana.valor == Decimal("650.00")
    assert semana.status == Cobranca.Status.PAGO


def test_d02_troca_sem_valor_ajustado_nao_mexe(alocacao):
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 8))
    _trocar(alocacao, date(2026, 7, 10), valor=None)
    assert Cobranca.objects.get(vencimento=date(2026, 7, 8)).valor == Decimal("650.00")


# ---------- revisão: casos de borda ----------


def test_d04_transicao_que_so_pagaria_dias_depois_da_devolucao_nao_e_devida(alocacao):
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 16))  # 15/07 cobre 15–21
    alocacao.dia_vencimento = 4  # sexta
    alocacao.save()
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 17))  # 17/07 cobre 22–23
    alocacao.encerrar(data_termino=date(2026, 7, 20), km_devolucao=50_100)
    transicao = Cobranca.objects.get(vencimento=date(2026, 7, 17))
    assert transicao.status == Cobranca.Status.CANCELADA
    assert Cobranca.objects.get(vencimento=date(2026, 7, 15)).status != Cobranca.Status.CANCELADA


def test_d04_rotina_atrasada_nao_lanca_transicao_depois_da_devolucao(alocacao):
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 16))
    alocacao.dia_vencimento = 4
    alocacao.save()
    # rotina parada em 17/07; carro devolvido na segunda 20/07
    alocacao.encerrar(data_termino=date(2026, 7, 20), km_devolucao=50_100)
    assert services.gerar_cobrancas_semanais(hoje=date(2026, 7, 21)) == []


def test_d02_cobranca_antiga_sem_periodo_tambem_reajusta(alocacao):
    # cobrança lançada antes da migração: sem período = semana do vencimento
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 8))
    Cobranca.objects.filter(vencimento=date(2026, 7, 8)).update(
        periodo_inicio=None, periodo_fim=None
    )
    _trocar(alocacao, date(2026, 7, 10))
    assert Cobranca.objects.get(vencimento=date(2026, 7, 8)).valor == Decimal("721.43")


def test_d02_semana_parcial_reajusta_e_mantem_o_pago(alocacao, cliente):
    services.gerar_cobrancas_semanais(hoje=date(2026, 7, 8))
    semana = Cobranca.objects.get(vencimento=date(2026, 7, 8))
    services.registrar_recebimento(
        cliente, date(2026, 7, 8), Decimal("300.00"), "pix", [(semana, Decimal("300.00"))]
    )
    _trocar(alocacao, date(2026, 7, 10))
    semana.refresh_from_db()
    assert semana.valor == Decimal("721.43")
    assert semana.saldo == Decimal("421.43")
    assert semana.status == Cobranca.Status.ATRASADO  # venceu em julho, ainda devendo
