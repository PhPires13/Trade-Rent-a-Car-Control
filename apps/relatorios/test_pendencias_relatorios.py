"""Pendências da frente relatórios: adiantamentos, venda no "Entrou", D07 no custo/km, D09."""

import csv
import io
from datetime import date
from decimal import Decimal

import pytest

from apps.financeiro.models import (
    AplicacaoRecebimento,
    Cobranca,
    MovimentoCredito,
    Recebimento,
)
from apps.frota import desmobilizacao
from apps.frota.models import Veiculo
from apps.km.models import RegistroKm
from apps.manutencao.models import Manutencao
from apps.pessoas.models import Cliente
from apps.relatorios import services
from apps.sinistros.models import Sinistro

HOJE = date(2026, 10, 26)


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(
        placa="QXQ6P01",
        marca_modelo="Gol",
        data_aquisicao=date(2025, 1, 1),
        valor_compra=Decimal("40000.00"),
    )


# --- adiantamentos: aparecem fora do total, iguais ao DAS ---------------------------


def _adiantamento(cliente, valor, data):
    recebimento = Recebimento.objects.create(cliente=cliente, data=data, valor=valor)
    MovimentoCredito.objects.create(
        cliente=cliente,
        tipo=MovimentoCredito.Tipo.ENTRADA,
        valor=valor,
        data=data,
        recebimento=recebimento,
    )


def test_adiantamento_aparece_na_tela_fora_do_total(usuario_logado, cliente):
    _adiantamento(cliente, Decimal("350.00"), date(2026, 9, 10))
    receitas = services.receitas_do_mes(2026, 9)
    assert receitas["adiantamentos"] == Decimal("350.00")
    # regra D13: entra na base/total só quando o crédito é usado
    assert receitas["total_receitas"] == Decimal("0")
    assert services.serie_mensal(2026, 9, quantidade=1)[0]["receita"] == 0.0

    html = usuario_logado.get("/relatorios/", {"mes": "2026-09"}).content.decode()
    assert "data-adiantamentos" in html
    assert "Adiantamentos recebidos" in html
    assert "R$ 350,00" in html


def test_planilha_de_receitas_lista_o_adiantamento_depois_do_total(usuario_logado, cliente):
    _adiantamento(cliente, Decimal("350.00"), date(2026, 9, 10))
    resposta = usuario_logado.get(
        "/relatorios/", {"mes": "2026-09", "exportar": "receitas", "formato": "csv"}
    )
    linhas = list(csv.reader(io.StringIO(resposta.content.decode("utf-8-sig")), delimiter=";"))
    grupos = [linha[0] for linha in linhas]
    i_total = grupos.index("TOTAL DAS RECEITAS")
    i_adiantamento = next(i for i, g in enumerate(grupos) if g.startswith("Adiantamentos"))
    assert i_adiantamento > i_total
    assert linhas[i_adiantamento][1:] == ["350,00", "Não"]


# --- "Entrou" com venda de carro avisa ----------------------------------------------


def test_entrou_avisa_quando_inclui_venda(usuario_logado, veiculo):
    veiculo.data_venda = date(2026, 9, 15)
    veiculo.valor_venda = Decimal("38000.00")
    veiculo.status = Veiculo.Status.VENDIDO
    veiculo.save()
    html = usuario_logado.get("/relatorios/", {"mes": "2026-09"}).content.decode()
    assert "data-entrou-venda" in html
    assert "Inclui a venda de carro (R$ 38.000,00)" in html


def test_entrou_sem_venda_nao_mostra_aviso(usuario_logado, veiculo):
    html = usuario_logado.get("/relatorios/", {"mes": "2026-09"}).content.decode()
    assert "data-entrou-venda" not in html


# --- D07 no custo de manutenção/km: conserto pago pela cota não pesa ------------------


def _km_rodado(veiculo, km):
    RegistroKm.objects.bulk_create(
        [
            RegistroKm(
                veiculo=veiculo,
                mes_referencia=date(2026, 9, 1),
                data_leitura=date(2026, 9, 30),
                km=10_000 + km,
                km_anterior=10_000,
            )
        ]
    )


def _sinistro_com_conserto_pela_cota(veiculo):
    sinistro = Sinistro.objects.create(
        veiculo=veiculo,
        data=date(2026, 9, 5),
        envolvido="terceiro",
        acionou_protecao=True,
        data_evento=date(2026, 9, 5),
        franquia_valor=Decimal("1500.00"),
    )
    Manutencao.objects.create(
        veiculo=veiculo,
        tipo="corretiva",
        data=date(2026, 9, 20),
        descricao="Funilaria pela proteção",
        origem_custo=Manutencao.OrigemCusto.PROTECAO,
        custo_real=Decimal("5000.00"),
        sinistro=sinistro,
    )
    Manutencao.objects.create(
        veiculo=veiculo,
        tipo="corretiva",
        data=date(2026, 9, 21),
        descricao="Farol (por fora)",
        custo_real=Decimal("300.00"),
    )
    return sinistro


def test_custo_por_km_ignora_conserto_pago_pela_cota(veiculo):
    _km_rodado(veiculo, 3000)
    _sinistro_com_conserto_pela_cota(veiculo)
    ficha = desmobilizacao.montar_ficha(veiculo, HOJE)
    assert ficha.custo_manutencao_6m == Decimal("300.00")
    assert ficha.custo_por_km_6m == Decimal("0.10")
    # a média da frota usa o mesmo critério
    assert desmobilizacao.media_custo_km_frota(HOJE) == Decimal("0.10")


def test_custo_por_km_conta_conserto_pela_protecao_sem_franquia(veiculo):
    _km_rodado(veiculo, 1000)
    Manutencao.objects.create(
        veiculo=veiculo,
        tipo="corretiva",
        data=date(2026, 9, 20),
        descricao="Evento sem sinistro cadastrado",
        origem_custo=Manutencao.OrigemCusto.PROTECAO,
        custo_real=Decimal("900.00"),
    )
    assert desmobilizacao.montar_ficha(veiculo, HOJE).custo_manutencao_6m == Decimal("900.00")
    assert desmobilizacao.media_custo_km_frota(HOJE) == Decimal("0.9")


# --- D09: franquia repassada ao cliente anula a despesa -----------------------------


def test_d09_franquia_cobrada_do_cliente_e_receita_do_carro_na_ficha(veiculo, cliente):
    sinistro = _sinistro_com_conserto_pela_cota(veiculo)
    cobranca = Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.REPASSE_SINISTRO,
        descricao="Franquia do sinistro",
        valor=Decimal("1500.00"),
        vencimento=date(2026, 9, 25),
    )
    sinistro.cobranca_franquia = cobranca
    sinistro.save(update_fields=["cobranca_franquia"])
    recebimento = Recebimento.objects.create(
        cliente=cliente, data=date(2026, 9, 25), valor=Decimal("1500.00")
    )
    AplicacaoRecebimento.objects.create(
        recebimento=recebimento, cobranca=cobranca, valor=Decimal("1500.00")
    )

    ficha = desmobilizacao.montar_ficha(veiculo, HOJE)
    assert ficha.despesa_franquias == Decimal("1500.00")
    assert ficha.receita_repasses == Decimal("1500.00")
    assert ficha.receita_aluguel == Decimal("0")

    # no relatório do mês a franquia (despesa) e o repasse (ND) também se anulam
    receitas = services.receitas_do_mes(2026, 9)
    despesas = services.despesas_do_mes(2026, 9)
    assert receitas["diversos"] == {Cobranca.Origem.REPASSE_SINISTRO.label: Decimal("1500.00")}
    assert despesas["total_franquias"] == Decimal("1500.00")


def _franquia_paga(veiculo, cliente, sinistro, alocacao=None):
    cobranca = Cobranca.objects.create(
        cliente=cliente,
        alocacao=alocacao,
        origem=Cobranca.Origem.REPASSE_SINISTRO,
        descricao="Franquia do sinistro",
        valor=Decimal("1500.00"),
        vencimento=date(2026, 9, 25),
    )
    sinistro.cobranca_franquia = cobranca
    sinistro.save(update_fields=["cobranca_franquia"])
    recebimento = Recebimento.objects.create(
        cliente=cliente, data=date(2026, 9, 25), valor=Decimal("1500.00")
    )
    AplicacaoRecebimento.objects.create(
        recebimento=recebimento, cobranca=cobranca, valor=Decimal("1500.00")
    )


def test_d09_franquia_com_alocacao_nao_conta_duas_vezes(veiculo, cliente):
    from apps.alocacoes.models import Alocacao

    alocacao = Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 9, 1),
        valor_semanal=Decimal("500.00"),
        km_entrega=10_000,
    )
    sinistro = _sinistro_com_conserto_pela_cota(veiculo)
    _franquia_paga(veiculo, cliente, sinistro, alocacao=alocacao)
    ficha = desmobilizacao.montar_ficha(veiculo, HOJE)
    # já entra pela alocação; não repete nos repasses
    assert ficha.receita_aluguel + ficha.receita_repasses == Decimal("1500.00")


def test_d09_repasse_de_manutencao_e_franquia_somam_no_mesmo_carro(veiculo, cliente):
    sinistro = _sinistro_com_conserto_pela_cota(veiculo)
    _franquia_paga(veiculo, cliente, sinistro)
    farol = Manutencao.objects.get(descricao="Farol (por fora)")
    cobranca = Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.REPASSE_MANUTENCAO,
        descricao="Farol",
        valor=Decimal("300.00"),
        vencimento=date(2026, 9, 25),
    )
    farol.cobranca_repasse = cobranca
    farol.save(update_fields=["cobranca_repasse"])
    recebimento = Recebimento.objects.create(
        cliente=cliente, data=date(2026, 9, 26), valor=Decimal("300.00")
    )
    AplicacaoRecebimento.objects.create(
        recebimento=recebimento, cobranca=cobranca, valor=Decimal("300.00")
    )
    assert desmobilizacao.montar_ficha(veiculo, HOJE).receita_repasses == Decimal("1800.00")


def test_ficha_do_carro_explica_que_repasse_inclui_franquia(usuario_logado, veiculo):
    html = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/ficha/").content.decode()
    assert "Consertos e franquias cobrados dos clientes" in html
