"""Regressões dos achados confirmados na revisão adversarial da etapa 8."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.alocacoes.models import Alocacao
from apps.financeiro import services as financeiro
from apps.financeiro.models import Cobranca
from apps.frota import desmobilizacao
from apps.frota.models import Veiculo
from apps.km.models import RegistroKm
from apps.manutencao.models import ItemPreventiva, Manutencao
from apps.multas.models import Multa
from apps.pessoas.models import Cliente
from apps.sinistros.models import AuxilioMotorista, Sinistro


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_atual=95_000)


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


def _dados_edicao(veiculo, **extras):
    dados = {
        "placa": veiculo.placa,
        "marca_modelo": veiculo.marca_modelo,
        "renavam": "",
        "chassi": "",
        "ano": "",
        "uso": veiculo.uso,
        "chave_reserva": veiculo.chave_reserva,
        "km_atual": veiculo.km_atual,
        "observacoes": "",
    }
    dados.update(extras)
    return dados


def test_status_nao_e_editavel_pelo_formulario(usuario_logado, veiculo, cliente):
    """O status é gerido pelos fluxos — editar por form dessincronizava a alocação."""
    Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=95_000,
    )
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.ALOCADO
    resposta = usuario_logado.post(
        f"/frota/veiculo/{veiculo.pk}/editar/",
        _dados_edicao(veiculo, status="disponivel"),  # campo extra é ignorado
    )
    assert resposta.status_code == 302
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.ALOCADO  # intacto


def test_km_atual_nao_pode_diminuir_na_edicao(usuario_logado, veiculo):
    resposta = usuario_logado.post(
        f"/frota/veiculo/{veiculo.pk}/editar/",
        _dados_edicao(veiculo, km_atual=9_500),
    )
    assert resposta.status_code == 200  # volta com erro no form
    assert "não diminui" in resposta.content.decode()
    veiculo.refresh_from_db()
    assert veiculo.km_atual == 95_000


def test_hub_ignora_preventiva_de_veiculo_inativo(usuario_logado, veiculo, db):
    """Mesma regra do painel: inativos fora dos alertas de preventiva."""
    oleo = ItemPreventiva.objects.get(nome="Troca de óleo e filtro")
    Manutencao.objects.create(
        veiculo=veiculo,
        item=oleo,
        tipo="preventiva",
        data=date(2026, 1, 1),
        km=80_000,
        descricao="Troca de óleo",  # vencida há 5.000 km
    )
    Veiculo.objects.filter(pk=veiculo.pk).update(status=Veiculo.Status.INATIVO)
    resposta = usuario_logado.get("/frota/?status=inativo")
    assert "preventiva em alerta" not in resposta.content.decode()


def test_hub_roda_com_queries_fixas(usuario_logado, cliente, django_assert_max_num_queries, db):
    """O nº de queries do hub não cresce com a frota (revisão etapa 8)."""
    oleo = ItemPreventiva.objects.get(nome="Troca de óleo e filtro")
    for indice in range(12):
        veiculo = Veiculo.objects.create(
            placa=f"TQ{indice:02d}A{indice:02d}", marca_modelo="Gol", km_atual=90_000
        )
        Manutencao.objects.create(
            veiculo=veiculo,
            item=oleo,
            tipo="preventiva",
            data=date(2026, 1, 1),
            km=80_000,
            descricao="Óleo",
        )
    with django_assert_max_num_queries(12):
        resposta = usuario_logado.get("/frota/")
    assert resposta.status_code == 200


def test_post_com_id_invalido_vira_404(usuario_logado, db):
    resposta = usuario_logado.post(
        "/frota/categorias/",
        {"categoria_id": "abc", "nome": "X", "valor_semanal_referencia": "1"},
    )
    assert resposta.status_code == 404


# --- Ficha de desmobilização em lote (revisão de performance) ---------------

HOJE = date(2026, 8, 10)


def _frota_de_teste(quantidade, item_preventiva=None):
    """Veículos com leituras de KM e manutenção, para provar o custo fixo."""
    veiculos = []
    for indice in range(quantidade):
        veiculo = Veiculo.objects.create(
            placa=f"TQ{indice:02d}A{indice:02d}",
            marca_modelo="Gol",
            km_atual=63_000,
            km_compra=60_000,
            data_aquisicao=date(2024, 7, 1),
            valor_compra=Decimal("42000.00"),
            mensalidade_protecao=Decimal("304.00"),
        )
        Manutencao.objects.create(
            veiculo=veiculo,
            item=item_preventiva,
            tipo="preventiva" if item_preventiva else "corretiva",
            data=HOJE - timedelta(days=20),
            km=62_000 if item_preventiva else None,
            descricao="Serviço",
            custo_real=Decimal("300.00"),
        )
        RegistroKm.objects.create(
            veiculo=veiculo,
            mes_referencia=date(2026, 7, 1),
            data_leitura=date(2026, 7, 10),
            km=63_000,
        )
        veiculos.append(veiculo)
    return veiculos


def test_ficha_em_lote_soma_cada_veiculo_sem_misturar(cliente, db):
    """Os números da ficha não podem mudar com o cálculo em lote (sem fan-out de join)."""
    carro = Veiculo.objects.create(
        placa="AAA1A11",
        marca_modelo="Gol",
        km_atual=63_000,
        km_compra=50_000,
        valor_compra=Decimal("40000.00"),
        custos_entrada=Decimal("2000.00"),
    )
    vazio = Veiculo.objects.create(placa="BBB2B22", marca_modelo="Voyage", km_atual=50_000)

    # dois sinistros, com auxílio recebido e não recebido no mesmo sinistro
    primeiro = Sinistro.objects.create(
        veiculo=carro,
        data=HOJE - timedelta(days=40),
        envolvido="terceiro",
        franquia_valor=Decimal("1500.00"),
    )
    segundo = Sinistro.objects.create(
        veiculo=carro,
        data=HOJE - timedelta(days=30),
        envolvido="terceiro",
        franquia_valor=Decimal("800.00"),
    )
    AuxilioMotorista.objects.create(sinistro=primeiro, valor=Decimal("500.00"), status="recebido")
    AuxilioMotorista.objects.create(sinistro=primeiro, valor=Decimal("300.00"), status="solicitado")
    AuxilioMotorista.objects.create(sinistro=segundo, valor=Decimal("200.00"), status="recebido")

    # só a multa da empresa é despesa do carro
    Multa.objects.create(
        veiculo=carro,
        data_infracao=HOJE - timedelta(days=25),
        valor=Decimal("130.00"),
        responsavel="empresa",
    )
    Multa.objects.create(
        veiculo=carro,
        data_infracao=HOJE - timedelta(days=25),
        valor=Decimal("200.00"),
        responsavel="cliente",
    )

    Manutencao.objects.create(
        veiculo=carro,
        tipo="esporadica",
        data=HOJE - timedelta(days=30),
        descricao="Suspensão",
        custo_real=Decimal("300.00"),
        data_entrada=HOJE - timedelta(days=35),
        data_saida=HOJE - timedelta(days=30),
    )
    Manutencao.objects.create(
        veiculo=carro,
        tipo="corretiva",
        data=HOJE - timedelta(days=10),
        descricao="Freios",
        custo_real=Decimal("100.00"),
    )
    Manutencao.objects.create(  # fora da janela de 6 meses
        veiculo=carro,
        tipo="corretiva",
        data=HOJE - timedelta(days=300),
        descricao="Embreagem",
        custo_real=Decimal("1000.00"),
    )

    # repasse de manutenção recebido é receita do carro
    repasse = Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.REPASSE_MANUTENCAO,
        descricao="Repasse",
        valor=Decimal("250.00"),
        vencimento=HOJE - timedelta(days=5),
    )
    Manutencao.objects.create(
        veiculo=carro,
        tipo="corretiva",
        data=HOJE - timedelta(days=300),
        descricao="Pneu do cliente",
        cobranca_repasse=repasse,
    )
    financeiro.registrar_recebimento(
        cliente, HOJE, Decimal("250.00"), "pix", [(repasse, Decimal("250.00"))]
    )

    RegistroKm.objects.create(  # fora da janela: não entra no km rodado dos 6 meses
        veiculo=carro, mes_referencia=date(2025, 1, 1), data_leitura=date(2025, 1, 10), km=55_000
    )
    RegistroKm.objects.create(
        veiculo=carro, mes_referencia=date(2026, 6, 1), data_leitura=date(2026, 6, 10), km=62_000
    )
    RegistroKm.objects.create(
        veiculo=carro, mes_referencia=date(2026, 7, 1), data_leitura=date(2026, 7, 10), km=63_000
    )

    ficha, ficha_vazio = desmobilizacao.montar_fichas_em_lote([carro, vazio], hoje=HOJE)
    assert ficha.investido == Decimal("42000.00")
    assert ficha.receita_repasses == Decimal("250.00")
    assert ficha.receita_auxilios == Decimal("700.00")  # só os recebidos
    assert ficha.despesa_franquias == Decimal("2300.00")  # 2 sinistros, sem multiplicar
    assert ficha.despesa_multas_empresa == Decimal("130.00")
    assert ficha.despesa_manutencao == Decimal("1400.00")
    assert ficha.custo_manutencao_6m == Decimal("400.00")  # sem a de 300 dias atrás
    assert ficha.dias_parado_6m == 5
    assert ficha.esporadicas_6m == 1
    assert ficha.km_rodado_6m == 8_000  # 7.000 + 1.000, sem os 5.000 da leitura antiga
    # o veículo sem movimento não herda nada do outro
    assert ficha_vazio.receita_total == Decimal("0")
    assert ficha_vazio.despesa_total == Decimal("0")
    assert ficha_vazio.km_rodado_6m == 0

    # a ficha de um carro só (tela da ficha) devolve exatamente os mesmos números
    individual = desmobilizacao.montar_ficha(carro, hoje=HOJE)
    for campo in vars(ficha):
        if campo not in ("motivos", "nivel"):
            assert getattr(individual, campo) == getattr(ficha, campo), campo


def test_ranking_da_frota_nao_cresce_com_a_frota(django_assert_max_num_queries, db):
    """O painel montava 10 queries por carro só para listar candidatos à venda."""
    _frota_de_teste(12)
    with django_assert_max_num_queries(12):
        fichas, media = desmobilizacao.ranking_da_frota(hoje=HOJE)
    assert len(fichas) == 12
    assert media is not None


def test_media_custo_km_e_a_mesma_do_ranking(django_assert_max_num_queries, db):
    """A ficha de um veículo usa a média barata — precisa bater com a do ranking."""
    _frota_de_teste(3)
    Veiculo.objects.create(placa="ZZZ9Z99", marca_modelo="HB20", km_atual=10_000)  # sem leitura
    _, media_do_ranking = desmobilizacao.ranking_da_frota(hoje=HOJE)
    with django_assert_max_num_queries(2):
        media = desmobilizacao.media_custo_km_frota(hoje=HOJE)
    assert media == media_do_ranking


def test_media_custo_km_sem_leituras_e_nula(veiculo, db):
    assert desmobilizacao.media_custo_km_frota(hoje=HOJE) is None


def test_ficha_de_um_veiculo_nao_monta_a_frota_inteira(
    usuario_logado, django_assert_max_num_queries, db
):
    """A tela da ficha rodava o ranking da frota inteira só para pegar a média."""
    veiculos = _frota_de_teste(12)
    with django_assert_max_num_queries(20):
        resposta = usuario_logado.get(f"/frota/veiculo/{veiculos[0].pk}/ficha/")
    assert resposta.status_code == 200
    assert veiculos[0].placa in resposta.content.decode()


def test_ranking_da_frota_na_tela_com_queries_fixas(
    usuario_logado, django_assert_max_num_queries, db
):
    """Inclui os vendidos, que também eram montados um a um."""
    veiculos = _frota_de_teste(12)
    for veiculo in veiculos[:3]:
        Veiculo.objects.filter(pk=veiculo.pk).update(
            status=Veiculo.Status.VENDIDO, data_venda=HOJE, valor_venda=Decimal("30000.00")
        )
    with django_assert_max_num_queries(25):
        resposta = usuario_logado.get("/frota/desmobilizacao/")
    assert resposta.status_code == 200
