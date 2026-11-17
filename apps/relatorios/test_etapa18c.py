"""Etapa 18c — Relatórios: tela e arquivo da contabilidade sem crescer com os
dados, o "baixar tudo" sem refazer o DAS três vezes e uma rede de segurança
que confere as três cópias das mesmas contas (série, mês a mês e ficha do carro).
"""

import random
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO

import openpyxl
import pytest
from django.db import connection
from django.db.models import Min
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.financeiro import services as financeiro
from apps.financeiro.models import (
    ZERO,
    Cobranca,
    MovimentacaoCaucao,
    MovimentoCredito,
    Recebimento,
)
from apps.frota.desmobilizacao import montar_ficha
from apps.frota.models import PagamentoIpva, Veiculo
from apps.manutencao.models import Manutencao
from apps.multas.models import Multa
from apps.pessoas.models import Cliente
from apps.relatorios import exportar, services, views
from apps.sinistros.models import AuxilioMotorista, Sinistro

HOJE = date(2026, 9, 20)

#: consultas da tela de Relatórios e do "baixar tudo" — o mesmo com 3 ou 12 carros
TETO_TELA = 43
TETO_TUDO = 35  # eram 47: o resumo do DAS era calculado 3 vezes


def _carro(i, **campos):
    dados = {
        "placa": f"TST{i:04d}",
        "marca_modelo": "Gol",
        "km_atual": 50_000,
        "data_aquisicao": date(2026, 1, 1) + timedelta(days=3 * i),
        "valor_compra": Decimal("40000.00"),
        "custos_entrada": Decimal("1500.00") + i,
        "mensalidade_protecao": Decimal("304.00"),
    }
    dados.update(campos)
    return Veiculo.objects.create(**dados)


def _montar_base(n, inicio=0):
    """n carros de locação, cada um com todas as fontes dos relatórios, e dois
    carros pessoais com gastos (que ficam fora). `inicio` acrescenta carros a
    uma base já montada (sem repetir o vendido e os pessoais)."""
    for i in range(inicio, n):
        carro = _carro(i)
        cliente = Cliente.objects.create(nome=f"Cliente {i}", cpf_cnpj=f"{i:03d}.222.333-44")
        alocacao = Alocacao.objects.create(
            veiculo=carro,
            cliente=cliente,
            data_inicio=date(2026, 6, 1) + timedelta(days=i % 20),
            valor_semanal=Decimal("650.00"),
            km_entrega=50_000,
        )
        caucao = financeiro.abrir_caucao(
            alocacao, valor_recebido=Decimal("1000.00"), data=alocacao.data_inicio
        )
        financeiro.gerar_cobrancas_semanais(hoje=HOJE, alocacao=alocacao)
        semanas = list(Cobranca.objects.filter(alocacao=alocacao).order_by("vencimento"))
        # 1ª semana pela caução, as do meio por Pix (uma com sobra que vira
        # crédito), as últimas ficam em aberto (recebíveis)
        financeiro.descontar_da_caucao(
            caucao, semanas[0], semanas[0].valor, semanas[0].vencimento, hoje=HOJE
        )
        for k, semana in enumerate(semanas[1:-2], start=1):
            sobra = Decimal("50.00") if k == 1 else Decimal("0")
            financeiro.registrar_recebimento(
                cliente,
                semana.vencimento,
                semana.valor + sobra,
                "pix",
                [(semana, semana.valor)],
                hoje=HOJE,
            )
        Manutencao.objects.create(
            veiculo=carro,
            tipo="corretiva",
            data=date(2026, 7, 1) + timedelta(days=i % 28),
            descricao=f"Conserto {i}",
            custo_real=Decimal("200.00") + i,
        )
        sinistro = Sinistro.objects.create(
            veiculo=carro,
            data=date(2026, 8, 1) + timedelta(days=i % 25),
            envolvido="terceiro",
            acionou_protecao=True,
            franquia_valor=Decimal("1500.00"),
            data_evento=None if i % 2 else date(2026, 8, 2) + timedelta(days=i % 25),
        )
        AuxilioMotorista.objects.create(
            sinistro=sinistro,
            valor=Decimal("700.00"),
            status=AuxilioMotorista.Status.RECEBIDO,
            data_recebimento=date(2026, 8, 20),
        )
        Multa.objects.create(
            veiculo=carro,
            data_infracao=date(2026, 7, 5) + timedelta(days=i % 20),
            valor=Decimal("130.16"),
            responsavel=Multa.Responsavel.EMPRESA,
        )
        PagamentoIpva.objects.create(
            veiculo=carro,
            ano=2026,
            parcela=1,
            valor=Decimal("900.00") + i,
            pago_em=date(2026, 2, 10) + timedelta(days=i),
        )
    if inicio:
        return
    # um carro vendido (receita e custo de venda), sem proteção depois da venda
    _carro(
        501,
        data_venda=date(2026, 8, 15),
        valor_venda=Decimal("35000.00"),
        custos_venda=Decimal("800.00"),
        status=Veiculo.Status.VENDIDO,
    )
    for j in range(2):
        pessoal = _carro(510 + j, uso=Veiculo.Uso.FORA_LOCACAO)
        Manutencao.objects.create(
            veiculo=pessoal,
            tipo="corretiva",
            data=date(2026, 8, 3),
            descricao="Pessoal",
            custo_real=Decimal("999.00"),
        )


def _extras(seed):
    """Casos que os testes isolados não juntam: cobrança avulsa sem alocação,
    pagamento com crédito, multa cancelada, conserto pago pela cota, IPVA não
    pago, carro sem proteção, vendido sem data e um carro que já foi alugado e
    virou "Fora de locação" (valores e datas variam com a seed)."""
    sorteio = random.Random(seed)
    carros = list(Veiculo.objects.filter(uso=Veiculo.Uso.LOCACAO).order_by("pk"))
    for carro in sorteio.sample(carros, 2):
        Multa.objects.create(
            veiculo=carro,
            data_infracao=date(2026, sorteio.randint(3, 8), sorteio.randint(1, 28)),
            valor=Decimal(sorteio.randint(88, 300)),
            responsavel=Multa.Responsavel.EMPRESA,
            resultado=Multa.Resultado.CANCELADA,
        )
    for carro in sorteio.sample(carros, 2):
        sinistro = Sinistro.objects.create(
            veiculo=carro,
            data=date(2026, sorteio.randint(2, 8), sorteio.randint(1, 28)),
            envolvido="associado",
            acionou_protecao=True,
            franquia_valor=Decimal(sorteio.randint(500, 3000)),
        )
        # conserto pago pela cota: a franquia já é a despesa
        Manutencao.objects.create(
            veiculo=carro,
            tipo="corretiva",
            data=sinistro.data,
            descricao="Funilaria pela proteção",
            custo_real=Decimal("4000.00"),
            origem_custo=Manutencao.OrigemCusto.PROTECAO,
            sinistro=sinistro,
        )
    PagamentoIpva.objects.create(
        veiculo=carros[0], ano=2026, parcela=2, valor=Decimal("450.00"), pago_em=None
    )
    compra = date(2026, sorteio.randint(1, 6), 10)
    _carro(900, data_aquisicao=compra, mensalidade_protecao=None, custos_entrada=None)
    _carro(
        901, data_aquisicao=compra, status=Veiculo.Status.VENDIDO, data_venda=None, valor_venda=None
    )
    # cobrança avulsa (sem alocação) paga por Pix
    cliente = Cliente.objects.order_by("pk").first()
    avulsa = Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.OUTRO,
        descricao="Lavagem",
        valor=Decimal(sorteio.randint(50, 150)),
        vencimento=date(2026, 8, 10),
    )
    financeiro.registrar_recebimento(
        cliente, date(2026, 8, 12), avulsa.valor, "pix", [(avulsa, avulsa.valor)], hoje=HOJE
    )
    # o cliente 1 tem R$ 50 de crédito (sobra): usa junto com o Pix da próxima semana
    com_credito = Cliente.objects.get(nome="Cliente 1")
    aberta = (
        Cobranca.objects.filter(cliente=com_credito, status__in=Cobranca.STATUS_DEVIDOS)
        .order_by("vencimento")
        .first()
    )
    financeiro.registrar_recebimento(
        com_credito,
        date(2026, 9, 15),
        aberta.valor - Decimal("50.00"),
        "pix",
        [(aberta, aberta.valor)],
        usar_credito=Decimal("50.00"),
        hoje=HOJE,
    )
    # carro alugado no passado que depois virou carro pessoal
    convertido = carros[-1]
    Veiculo.objects.filter(pk=convertido.pk).update(uso=Veiculo.Uso.FORA_LOCACAO)


def _consultas(funcao):
    with CaptureQueriesContext(connection) as contexto:
        resultado = funcao()
    return len(contexto.captured_queries), resultado


# --- primeiro mês com dado: uma consulta só --------------------------------


def _primeiro_mes_referencia():
    """A implementação antiga (12 consultas MIN), como referência."""
    menores = [
        Veiculo.objects.filter(uso=Veiculo.Uso.LOCACAO).aggregate(d=Min("data_aquisicao"))["d"],
        Veiculo.objects.filter(uso=Veiculo.Uso.LOCACAO).aggregate(d=Min("data_venda"))["d"],
        Alocacao.objects.aggregate(d=Min("data_inicio"))["d"],
        Recebimento.objects.aggregate(d=Min("data"))["d"],
        Cobranca.objects.aggregate(d=Min("vencimento"))["d"],
        MovimentacaoCaucao.objects.aggregate(d=Min("data"))["d"],
        Manutencao.objects.filter(veiculo__uso=Veiculo.Uso.LOCACAO).aggregate(d=Min("data"))["d"],
        Multa.objects.filter(veiculo__uso=Veiculo.Uso.LOCACAO).aggregate(d=Min("data_infracao"))[
            "d"
        ],
        Sinistro.objects.filter(veiculo__uso=Veiculo.Uso.LOCACAO).aggregate(d=Min("data"))["d"],
        Sinistro.objects.filter(veiculo__uso=Veiculo.Uso.LOCACAO).aggregate(d=Min("data_evento"))[
            "d"
        ],
        AuxilioMotorista.objects.filter(sinistro__veiculo__uso=Veiculo.Uso.LOCACAO).aggregate(
            d=Min("data_recebimento")
        )["d"],
        PagamentoIpva.objects.filter(veiculo__uso=Veiculo.Uso.LOCACAO).aggregate(d=Min("pago_em"))[
            "d"
        ],
    ]
    datas = [d for d in menores if d]
    return min(datas) if datas else None


def test_primeiro_mes_com_dado_banco_vazio(db, django_assert_num_queries):
    with django_assert_num_queries(1):
        assert services.primeiro_mes_com_dado() is None


@pytest.mark.parametrize(
    "criar",
    [
        lambda: Sinistro.objects.create(
            veiculo=_carro(1),
            data=date(2026, 6, 1),
            envolvido="terceiro",
            data_evento=date(2024, 3, 9),
        ),
        lambda: PagamentoIpva.objects.create(
            veiculo=_carro(2), ano=2023, parcela=1, valor=Decimal("1"), pago_em=date(2023, 2, 1)
        ),
        lambda: Manutencao.objects.create(
            veiculo=_carro(3, uso=Veiculo.Uso.FORA_LOCACAO, data_aquisicao=date(2020, 1, 1)),
            tipo="corretiva",
            data=date(2021, 5, 5),
            descricao="pessoal",
            custo_real=Decimal("10"),
        ),
        lambda: _montar_base(3),
    ],
    ids=["so_data_evento", "so_ipva", "so_carro_pessoal", "base_inteira"],
)
def test_primeiro_mes_com_dado_igual_ao_antigo_numa_consulta(db, django_assert_num_queries, criar):
    criar()
    esperado = _primeiro_mes_referencia()
    with django_assert_num_queries(1):
        assert services.primeiro_mes_com_dado() == esperado


# --- a tela e o arquivo não crescem com os dados ---------------------------


def test_tela_e_arquivo_nao_crescem_com_os_dados(usuario_logado):
    medidas = []
    for n in (3, 12):
        if n == 12:
            _montar_base(12, inicio=3)
        else:
            _montar_base(3)
        tela, resposta = _consultas(lambda: usuario_logado.get("/relatorios/", {"mes": "2026-08"}))
        assert resposta.status_code == 200
        tudo, resposta = _consultas(
            lambda: usuario_logado.get("/relatorios/", {"mes": "2026-08", "exportar": "tudo"})
        )
        assert resposta.status_code == 200
        medidas.append((tela, tudo))
    # o mesmo número de consultas com 3 e com 12 carros (sem consulta por linha)
    assert medidas[0] == medidas[1]
    tela, tudo = medidas[0]
    assert tela <= TETO_TELA
    assert tudo <= TETO_TUDO


# --- "baixar tudo": mesmas abas das planilhas soltas -----------------------


def _celulas(conteudo):
    livro = openpyxl.load_workbook(BytesIO(conteudo))
    return {
        aba.title: [[celula.value for celula in linha] for linha in aba.iter_rows()]
        for aba in livro.worksheets
    }


def test_baixar_tudo_igual_as_planilhas_soltas(usuario_logado):
    _montar_base(4)
    _extras(7)
    resposta = usuario_logado.get("/relatorios/", {"mes": "2026-08", "exportar": "tudo"})
    # referência: cada aba calculada sozinha, do zero (como nas exportações de uma aba)
    referencia = exportar.planilha_xlsx_abas(
        "ref",
        [
            views._planilha_receitas(2026, 8),
            views._planilha_despesas(2026, 8),
            views._planilha_das(2026, 8),
            views._planilha_recebiveis(2026, 8),
            views._planilha_frota(2026, 8),
            views._planilha_pagamentos_das(2026, 8),
        ],
    )
    abas = _celulas(resposta.content)
    assert abas == _celulas(referencia.content)
    # e a conta fecha: a base do DAS das abas é a locação da aba Receitas
    assert abas["Receitas"][1][1] == abas["DAS"][1][1] > 0


# --- rede de segurança: as três cópias das mesmas contas batem -------------


@pytest.mark.parametrize("seed", [11, 29])
def test_serie_mes_a_mes_e_ficha_do_carro_batem(db, seed):
    _montar_base(6)
    _extras(seed)
    meses = [(2026, m) for m in range(1, HOJE.month + 1)]
    serie = services.serie_mensal(2026, HOJE.month, quantidade=len(meses))
    por_carro = {}
    for (ano, mes), ponto in zip(meses, serie, strict=True):
        receitas = services.receitas_do_mes(ano, mes)
        despesas = services.despesas_do_mes(ano, mes)
        # (a) gráfico = quadro do mês, receita e despesa
        assert ponto["rotulo"] == f"{mes:02d}/{ano}"
        assert ponto["receita"] == float(receitas["total_receitas"])
        assert ponto["despesa"] == float(despesas["total_geral"])
        for lista, campo in (
            ("manutencoes", "custo_real"),
            ("franquias", "franquia_valor"),
            ("multas_empresa", "valor"),
            ("ipvas", "valor"),
        ):
            for item in despesas[lista]:
                por_carro[item.veiculo_id] = por_carro.get(item.veiculo_id, ZERO) + getattr(
                    item, campo
                )
        for carro in despesas["frota_protegida"]:
            por_carro[carro.pk] = por_carro.get(carro.pk, ZERO) + carro.mensalidade_protecao
    # (b) soma das despesas de cada carro mês a mês = despesa total da ficha do carro
    for carro in Veiculo.objects.filter(uso=Veiculo.Uso.LOCACAO):
        ficha = montar_ficha(carro, hoje=HOJE)
        assert por_carro.get(carro.pk, ZERO) == ficha.despesa_total, carro.placa


# --- carro que já foi alugado e virou "Fora de locação" ---------------------


def test_carro_que_virou_pessoal_gasto_sai_e_aluguel_faturado_fica_no_das(db):
    """Os gastos do carro saem dos relatórios (também nos meses passados), como
    avisa o cadastro. O aluguel que ele já rendeu continua na base do DAS: teve
    fatura e o imposto daquele mês não pode mudar depois (decisão nº 11). O
    gráfico e o quadro do mês contam do mesmo jeito."""
    _montar_base(2)
    carro = Veiculo.objects.get(placa="TST0001")
    antes = services.receitas_do_mes(2026, 7)
    despesas_antes = services.despesas_do_mes(2026, 7)
    serie_antes = services.serie_mensal(2026, 7, quantidade=1)[0]
    Veiculo.objects.filter(pk=carro.pk).update(uso=Veiculo.Uso.FORA_LOCACAO)

    depois = services.receitas_do_mes(2026, 7)
    despesas_depois = services.despesas_do_mes(2026, 7)
    serie_depois = services.serie_mensal(2026, 7, quantidade=1)[0]
    assert depois["locacao"] == antes["locacao"] > 0
    assert carro.pk not in {m.veiculo_id for m in despesas_depois["manutencoes"]}
    assert despesas_depois["total_geral"] < despesas_antes["total_geral"]
    assert serie_depois["receita"] == float(depois["total_receitas"])
    assert serie_depois["despesa"] == float(despesas_depois["total_geral"])
    assert serie_antes["receita"] == serie_depois["receita"]


# --- quem deve: o crédito do cliente sem o termo antigo --------------------


def test_quem_deve_mostra_credito_sem_termo_antigo(usuario_logado):
    """Na linha do próprio cliente basta "tem R$ 50,00 de crédito" (sem
    "guardado" e sem repetir "do cliente"), como na planilha de recebíveis."""
    hoje = timezone.localdate()
    cliente = Cliente.objects.create(nome="Ana Credito", cpf_cnpj="111.222.333-99")
    Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.ALUGUEL,
        descricao="Aluguel",
        valor=Decimal("650.00"),
        vencimento=hoje,
    )
    MovimentoCredito.objects.create(
        cliente=cliente, tipo=MovimentoCredito.Tipo.ENTRADA, valor=Decimal("50.00"), data=hoje
    )
    html = usuario_logado.get("/relatorios/").content.decode()
    assert "tem R$ 50,00 de crédito — descontando o crédito, falta R$ 600,00" in html
    assert "crédito guardado" not in html.lower()
    assert "de crédito do cliente — descontando" not in html
