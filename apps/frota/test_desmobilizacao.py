from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.alocacoes.models import Alocacao
from apps.financeiro import services as financeiro
from apps.financeiro.models import Cobranca
from apps.frota import desmobilizacao
from apps.frota.models import Veiculo
from apps.manutencao.models import Manutencao
from apps.pessoas.models import Cliente
from apps.sinistros.models import AuxilioMotorista, Sinistro


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(
        placa="QXQ6C10",
        marca_modelo="Gol",
        km_atual=95_000,
        data_aquisicao=date(2024, 7, 1),
        valor_compra=Decimal("42000.00"),
        custos_entrada=Decimal("2000.00"),
        mensalidade_protecao=Decimal("304.00"),
    )


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


@pytest.fixture
def alocacao(veiculo, cliente):
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=95_000,
    )


def test_ficha_soma_receitas_e_despesas(alocacao, veiculo, cliente):
    hoje = date(2026, 7, 20)
    financeiro.gerar_cobrancas_semanais(hoje=hoje)
    for cobranca in Cobranca.objects.filter(origem="aluguel"):
        financeiro.registrar_recebimento(
            cliente, cobranca.vencimento, cobranca.valor, "pix", [(cobranca, cobranca.valor)]
        )
    Manutencao.objects.create(
        veiculo=veiculo,
        tipo="corretiva",
        data=date(2026, 7, 10),
        descricao="Freios",
        custo_real=Decimal("400.00"),
    )
    sinistro = Sinistro.objects.create(
        veiculo=veiculo,
        data=date(2026, 7, 5),
        envolvido="terceiro",
        acionou_protecao=True,
        franquia_valor=Decimal("1500.00"),
    )
    AuxilioMotorista.objects.create(
        sinistro=sinistro,
        valor=Decimal("1518.00"),
        status="recebido",
        data_recebimento=date(2026, 7, 15),
    )
    ficha = desmobilizacao.montar_ficha(veiculo, hoje=hoje)
    assert ficha.investido == Decimal("44000.00")
    assert ficha.receita_aluguel == Decimal("1950.00")  # 3 semanas
    assert ficha.receita_auxilios == Decimal("1518.00")
    assert ficha.despesa_manutencao == Decimal("400.00")
    assert ficha.despesa_franquias == Decimal("1500.00")
    # proteção estimada: 24 meses × 304
    assert ficha.despesa_protecao_estimada == Decimal("7296.00")
    assert ficha.resultado_operacional == ficha.receita_total - ficha.despesa_total


def test_percentual_recuperado_dispara_janela_de_venda(veiculo, db):
    ficha = desmobilizacao.FichaFinanceira(
        veiculo=veiculo, investido=Decimal("40000"), receita_aluguel=Decimal("35000")
    )
    desmobilizacao.avaliar(ficha)
    assert ficha.nivel == "preparar"
    assert any("Recuperou" in m for m in ficha.motivos)


def test_criterios_acumulados_viram_vender(veiculo, db):
    ficha = desmobilizacao.FichaFinanceira(
        veiculo=veiculo,
        investido=Decimal("40000"),
        receita_aluguel=Decimal("35000"),
        dias_parado_6m=25,
    )
    desmobilizacao.avaliar(ficha)
    assert ficha.nivel == "vender"
    assert len(ficha.motivos) == 2


def test_sem_criterios_mantem(veiculo, db):
    ficha = desmobilizacao.FichaFinanceira(veiculo=veiculo, investido=Decimal("40000"))
    desmobilizacao.avaliar(ficha)
    assert ficha.nivel == "manter"
    assert ficha.motivos == []


def test_custo_km_acima_da_media_observar(veiculo, db):
    ficha = desmobilizacao.FichaFinanceira(
        veiculo=veiculo,
        investido=Decimal("40000"),
        custo_manutencao_6m=Decimal("3000"),
    )
    ficha.km_rodado_6m = 10_000  # custo/km = 0,30
    desmobilizacao.avaliar(ficha, media_custo_km_frota=Decimal("0.10"))
    assert ficha.nivel == "observar"


def test_venda_bloqueada_com_alocacao_ativa(alocacao, veiculo):
    with pytest.raises(ValidationError):
        desmobilizacao.registrar_venda(veiculo, date(2026, 8, 1), Decimal("35000"))


def test_venda_e_resultado_final(alocacao, veiculo):
    alocacao.encerrar(data_termino=date(2026, 7, 20), km_devolucao=96_000)
    desmobilizacao.registrar_venda(
        veiculo,
        date(2026, 8, 1),
        Decimal("35000.00"),
        comprador="João",
        custos=Decimal("500.00"),
        km=96_100,
    )
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.VENDIDO
    assert veiculo.km_atual == 96_100
    ficha = desmobilizacao.montar_ficha(veiculo, hoje=date(2026, 8, 1))
    # sem receitas/despesas: resultado final = 0 - protecao_estimada + 35000 - 500 - 44000
    esperado = ficha.resultado_operacional + Decimal("35000") - Decimal("500") - ficha.investido
    assert ficha.resultado_final == esperado
    with pytest.raises(ValidationError):
        desmobilizacao.registrar_venda(veiculo, date(2026, 8, 2), Decimal("1"))


def test_ranking_ordena_piores_primeiro(veiculo, cliente, db):
    bom = Veiculo.objects.create(
        placa="RNB9J66",
        marca_modelo="Voyage",
        valor_compra=Decimal("48000"),
        data_aquisicao=date(2026, 1, 1),
    )
    Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=95_000,
    )
    financeiro.gerar_cobrancas_semanais(hoje=date(2026, 7, 1))
    cobranca = Cobranca.objects.get(origem="aluguel")
    cobranca.valor = Decimal("45000.00")  # força % alto p/ teste
    cobranca.save()
    financeiro.registrar_recebimento(
        cliente, date(2026, 7, 1), Decimal("45000.00"), "pix", [(cobranca, Decimal("45000.00"))]
    )
    fichas, _ = desmobilizacao.ranking_da_frota(hoje=date(2026, 7, 20))
    assert fichas[0].veiculo == veiculo
    assert fichas[0].nivel in ("preparar", "vender")
    assert fichas[-1].veiculo == bom


@pytest.fixture
def usuario_logado(client, django_user_model):
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    client.login(username="dono", password="senha-forte-123")
    return client


def test_telas_de_desmobilizacao_renderizam(usuario_logado, veiculo):
    assert usuario_logado.get("/frota/desmobilizacao/").status_code == 200
    assert usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/ficha/").status_code == 200
    assert usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/vender/").status_code == 200
    assert usuario_logado.get("/").status_code == 200


def test_vender_pela_tela(usuario_logado, veiculo):
    resposta = usuario_logado.post(
        f"/frota/veiculo/{veiculo.pk}/vender/",
        {"data": "2026-08-01", "valor": "35000,00", "comprador": "João", "custos": "", "km": ""},
    )
    assert resposta.status_code == 302
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.VENDIDO
