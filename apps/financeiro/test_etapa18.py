"""Etapa 18 — financeiro: DAS que fecha com as linhas, inadimplência na hora
(cancelar / judicial), dica do encargo no Receber e conferência de recebimentos."""

import io
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.utils import timezone
from openpyxl import load_workbook

from apps.alocacoes.models import Alocacao
from apps.financeiro import services
from apps.financeiro.models import Cobranca, MovimentacaoCaucao, Recebimento
from apps.financeiro.views import linhas_base_das, linhas_pagamentos_das
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Rodrigo Caixa", cpf_cnpj="111.222.333-44")


@pytest.fixture
def alocacao(cliente):
    veiculo = Veiculo.objects.create(placa="RTB2C03", marca_modelo="Gol", km_atual=50_000)
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 8, 20),
        valor_semanal=Decimal("650.00"),
        km_entrega=50_000,
    )


def _cobranca(cliente, alocacao=None, valor="650.00", vencimento=date(2026, 9, 2), **extra):
    extra.setdefault("origem", Cobranca.Origem.ALUGUEL)
    extra.setdefault("descricao", "Aluguel semanal RTB2C03")
    return Cobranca.objects.create(
        cliente=cliente,
        alocacao=alocacao,
        valor=Decimal(valor),
        vencimento=vencimento,
        **extra,
    )


@pytest.fixture
def setembro(cliente, alocacao):
    """Setembro com: depósito de caução (1.000 — único dinheiro novo), pagamento só com
    crédito cuja sobra virou reforço de caução (200) e um desconto na caução (500)."""
    agosto = _cobranca(cliente, alocacao, valor="300.00", vencimento=date(2026, 8, 20))
    # agosto: Pix de 800 para 300 → sobra 500 de crédito (adiantamento de agosto)
    services.registrar_recebimento(
        cliente, date(2026, 8, 25), Decimal("800.00"), "pix", [(agosto, "300.00")]
    )
    caucao = services.abrir_caucao(alocacao, Decimal("1000.00"), data=date(2026, 9, 1), forma="pix")
    paga_com_credito = _cobranca(cliente, alocacao, valor="300.00", vencimento=date(2026, 9, 2))
    services.registrar_recebimento(
        cliente,
        date(2026, 9, 2),
        Decimal("500.00"),
        "credito",
        [(paga_com_credito, "300.00")],
        sobra_destino="caucao",
    )
    descontada = _cobranca(cliente, alocacao, valor="650.00", vencimento=date(2026, 9, 9))
    services.descontar_da_caucao(caucao, descontada, Decimal("500.00"), date(2026, 9, 10))
    return caucao


# ---------- DAS: as linhas fecham com o "Total que entrou no caixa" ----------


def test_resumo_fiscal_separa_desconto_e_reforco_de_credito(setembro):
    r = services.resumo_fiscal(2026, 9)
    assert r["locacao"] == Decimal("800.00")
    assert r["pago_com_credito"] == Decimal("300.00")
    assert r["descontado_da_caucao"] == Decimal("500.00")
    assert r["reforco_de_credito"] == Decimal("200.00")
    assert r["caucao_recebida"] == Decimal("1200.00")
    # só o depósito da caução foi dinheiro novo no banco
    assert r["entrou_no_caixa"] == Decimal("1000.00")


def test_reforco_feito_com_credito_nao_aparece_como_caucao_recebida(setembro):
    fora = services.entradas_fora_do_das(2026, 9)
    assert [linha["valor"] for linha in fora] == [Decimal("1000.00")]
    r = services.resumo_fiscal(2026, 9)
    assert sum(linha["valor"] for linha in fora) == (
        r["adiantamentos"] + r["caucao_recebida"] - r["reforco_de_credito"]
    )


def test_primeira_aba_soma_o_total_que_entrou_no_caixa(setembro):
    linhas = linhas_base_das(2026, 9)
    *parcelas, total = linhas
    assert total[0] == "Total que entrou no caixa"
    assert sum(valor for _, valor, _ in parcelas) == total[1] == Decimal("1000.00")
    rotulos = [rotulo for rotulo, _, _ in linhas]
    assert any("desconto na caução" in r for r in rotulos)
    assert any("reforço feito com crédito" in r for r in rotulos)


def test_segunda_aba_termina_no_total_que_entrou_no_caixa(setembro):
    linhas = linhas_pagamentos_das(2026, 9)
    assert linhas[-1][3] == "Total que entrou no caixa"
    assert linhas[-1][5] == Decimal("1000.00")
    # linhas de pagamento (com data) + ajustes "não é dinheiro novo" = total
    individuais = sum(linha[5] for linha in linhas if linha[0])
    ajustes = sum(linha[5] for linha in linhas if "não é dinheiro novo" in str(linha[3]))
    assert individuais + ajustes == linhas[-1][5]
    assert not any("Caução recebida" in str(linha[3]) and linha[5] == 200 for linha in linhas)


def test_tela_e_planilha_do_das_mostram_as_linhas_novas(usuario_logado, setembro):
    tela = usuario_logado.get("/financeiro/das/", {"mes": "2026-09"}).content.decode()
    assert "data-desconto-caucao" in tela and "data-reforco-credito" in tela
    resposta = usuario_logado.get("/financeiro/das/", {"mes": "2026-09", "exportar": "xlsx"})
    livro = load_workbook(io.BytesIO(resposta.content))
    base = [list(linha) for linha in livro.worksheets[0].iter_rows(values_only=True)][1:]
    assert sum(linha[1] for linha in base[:-1]) == pytest.approx(base[-1][1]) == 1000.0
    pagamentos = [list(linha) for linha in livro.worksheets[1].iter_rows(values_only=True)]
    assert pagamentos[-1][3] == "Total que entrou no caixa" and pagamentos[-1][5] == 1000.0


def test_mes_sem_ajustes_nao_ganha_linhas_vazias(cliente, alocacao):
    cobranca = _cobranca(cliente, alocacao, vencimento=date(2026, 9, 2))
    services.registrar_recebimento(
        cliente, date(2026, 9, 2), Decimal("650.00"), "pix", [(cobranca, "650.00")]
    )
    linhas = linhas_base_das(2026, 9)
    assert not any("não é dinheiro novo" in linha[0] for linha in linhas)
    assert linhas[-1][1] == Decimal("650.00")


# ---------- inadimplência atualizada na hora (cancelar e judicial) ----------


def test_cancelar_unica_divida_atrasada_volta_cliente_a_ativo(usuario_logado, cliente):
    hoje = timezone.localdate()
    excedente = _cobranca(
        cliente,
        valor="120.00",
        vencimento=hoje - timedelta(days=10),
        origem=Cobranca.Origem.EXCEDENTE_KM,
        descricao="Excedente de km",
    )
    services.marcar_atrasos(hoje)
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.INADIMPLENTE
    usuario_logado.post(f"/financeiro/cobranca/{excedente.pk}/cancelar/")
    excedente.refresh_from_db()
    cliente.refresh_from_db()
    assert excedente.status == Cobranca.Status.CANCELADA
    assert cliente.status == Cliente.Status.ATIVO


def test_cancelar_nao_libera_quem_tem_outra_divida_atrasada(usuario_logado, cliente):
    hoje = timezone.localdate()
    excedente = _cobranca(
        cliente,
        valor="120.00",
        vencimento=hoje - timedelta(days=10),
        origem=Cobranca.Origem.EXCEDENTE_KM,
        descricao="Excedente de km",
    )
    _cobranca(cliente, vencimento=hoje - timedelta(days=5))
    services.marcar_atrasos(hoje)
    usuario_logado.post(f"/financeiro/cobranca/{excedente.pk}/cancelar/")
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.INADIMPLENTE


def test_marcar_judicial_deixa_inadimplente_na_hora(usuario_logado, cliente):
    hoje = timezone.localdate()
    # a rotina ainda não rodou: cobrança atrasada, cliente ainda "Ativo"
    atrasada = _cobranca(
        cliente, vencimento=hoje - timedelta(days=1), status=Cobranca.Status.ATRASADO
    )
    Cliente.objects.filter(pk=cliente.pk).update(status=Cliente.Status.ATIVO)
    usuario_logado.post(f"/financeiro/cobranca/{atrasada.pk}/judicial/")
    atrasada.refresh_from_db()
    cliente.refresh_from_db()
    assert atrasada.status == Cobranca.Status.JUDICIAL
    assert cliente.status == Cliente.Status.INADIMPLENTE


def test_judicial_em_cobranca_paga_continua_recusado(usuario_logado, cliente):
    paga = _cobranca(cliente, status=Cobranca.Status.PAGO)
    resposta = usuario_logado.post(f"/financeiro/cobranca/{paga.pk}/judicial/", follow=True)
    paga.refresh_from_db()
    assert paga.status == Cobranca.Status.PAGO
    assert "Só cobrança em aberto" in resposta.content.decode()


# ---------- Receber: dica de cobrar o encargo antes do pagamento parcial ----------


def test_receber_avisa_que_o_encargo_fica_menor_depois(usuario_logado, cliente, alocacao):
    hoje = timezone.localdate()
    _cobranca(
        cliente, alocacao, vencimento=hoje - timedelta(days=3), status=Cobranca.Status.ATRASADO
    )
    tela = usuario_logado.get("/financeiro/receber/", {"cliente": cliente.pk}).content.decode()
    assert "data-dica-encargo" in tela
    assert "Atrasada há 3 dias" in tela


# ---------- desfazer de outro dia: sem mandar para um caminho que não existe ----------


def test_desfazer_de_outro_dia_nao_manda_para_a_retaguarda(cliente):
    cobranca = _cobranca(cliente, vencimento=date(2026, 9, 2))
    recebimento = services.registrar_recebimento(
        cliente, date(2026, 9, 2), Decimal("650.00"), "pix", [(cobranca, "650.00")]
    )
    amanha = timezone.localdate() + timedelta(days=1)
    with pytest.raises(Exception) as erro:
        services.desfazer_recebimento(recebimento, hoje=amanha)
    assert "Retaguarda" not in str(erro.value)
    assert "mesmo dia" in str(erro.value)


# ---------- conferência de recebimentos sem destino ----------


def test_lancamentos_pelas_telas_sempre_fecham(setembro):
    assert services.recebimentos_incoerentes() == []


def test_recebimento_sem_destino_e_acusado_na_rotina(cliente):
    solto = Recebimento.objects.create(
        cliente=cliente, data=date(2026, 9, 2), valor=Decimal("7.77"), forma="pix"
    )
    assert services.recebimentos_incoerentes() == [(solto, Decimal("7.77"))]
    erros = io.StringIO()
    call_command("rotina_diaria", stdout=io.StringIO(), stderr=erros)
    assert f"recebimento nº {solto.pk}" in erros.getvalue()
    assert "R$ 7,77" in erros.getvalue()


# ---------- painel: o card da semana diz que conta pelo vencimento ----------


def test_painel_rotulo_da_semana_pelo_vencimento(usuario_logado):
    tela = usuario_logado.get("/").content.decode()
    assert "Já pago do que vence nesta semana" in tela
    assert "Recebido nesta semana" not in tela


def test_reforco_de_credito_continua_com_a_marca_do_recebimento(setembro):
    reforco = MovimentacaoCaucao.objects.get(tipo=MovimentacaoCaucao.Tipo.REFORCO)
    assert reforco.observacoes.startswith("Sobra do recebimento nº")


# ---------- Emitir ND: cliente que sumiu não perde os itens digitados ----------


def test_nd_com_cliente_inexistente_volta_com_os_itens(usuario_logado, cliente):
    resposta = usuario_logado.post(
        "/financeiro/nd/nova/",
        {
            "token": "t-nd-sumiu",
            "cliente": "999999",
            "data_emissao": timezone.localdate().isoformat(),
            "item_descricao": ["Avaria porta direita"],
            "item_valor": ["300,00"],
        },
    )
    assert resposta.status_code == 200
    tela = resposta.content.decode()
    assert "escolha o cliente da nota" in tela
    assert "Avaria porta direita" in tela


# ---------- revisão: ordem das travas e layout do DAS no celular ----------


def _tabelas_das_consultas(contexto):
    """Tabela do FROM de cada SELECT, na ordem em que rodaram."""
    return [
        q["sql"].split(" FROM ", 1)[1].split()[0].strip('"')
        for q in contexto.captured_queries
        if q["sql"].startswith("SELECT") and " FROM " in q["sql"]
    ]


def test_acoes_do_painel_travam_o_cliente_antes_da_cobranca(cliente):
    """Mesma ordem do Receber (cliente → cobrança): em ordem trocada, um "não
    cobrar" e um pagamento ao mesmo tempo se esperariam para sempre no PostgreSQL."""
    from django.db import connection, transaction
    from django.test.utils import CaptureQueriesContext

    from apps.financeiro.views import _cobranca_travada

    cobranca = _cobranca(cliente, origem=Cobranca.Origem.OUTRO, descricao="Avulsa")
    with transaction.atomic(), CaptureQueriesContext(connection) as contexto:
        travada = _cobranca_travada(cobranca)
    assert travada.pk == cobranca.pk
    tabelas = _tabelas_das_consultas(contexto)
    assert tabelas[:2] == ["pessoas_cliente", "financeiro_cobranca"]


def test_encargo_trava_o_cliente_antes_do_aluguel(cliente, alocacao):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    hoje = timezone.localdate()
    aluguel = _cobranca(cliente, alocacao, vencimento=hoje - timedelta(days=2))
    aluguel.atualizar_status(hoje)
    with CaptureQueriesContext(connection) as contexto:
        services.aplicar_encargo(aluguel, Decimal("30.00"), hoje)
    tabelas = [t for t in _tabelas_das_consultas(contexto) if t.startswith(("pessoas", "fin"))]
    assert tabelas[:2] == ["pessoas_cliente", "financeiro_cobranca"]


def test_das_no_celular_valores_negativos_nao_quebram_linha(usuario_logado, setembro):
    """ "− R$ 500,00" numa coluna estreita (375px) não pode partir o sinal do valor."""
    tela = usuario_logado.get("/financeiro/das/", {"mes": "2026-09"}).content.decode()
    assert tela.count('whitespace-nowrap px-4 py-2 text-right">− ') == 3
    assert 'class="px-4 py-2 text-right' not in tela
