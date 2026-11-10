"""Etapa 18 — relatórios: caixinha "Ir para" com todas as fontes, recebíveis
com crédito maior que a dívida, setas de mês iguais às do DAS e a mesma aba
de pagamentos do DAS em Relatórios e em Financeiro › DAS."""

import io
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from openpyxl import load_workbook

from apps.alocacoes.models import Alocacao
from apps.financeiro.models import Caucao, Cobranca, MovimentacaoCaucao, MovimentoCredito
from apps.frota.models import Veiculo
from apps.manutencao.models import Manutencao
from apps.pessoas.models import Cliente
from apps.relatorios import services, views


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


@pytest.fixture
def veiculo(db):
    # sem data de compra, sem aluguel e sem pagamento: só a despesa/cobrança conta
    return Veiculo.objects.create(placa="QXQ7R28", marca_modelo="Gol")


def _params(opcoes):
    return [o["param"] for o in opcoes]


def test_ir_para_inclui_mes_de_manutencao_antiga(veiculo):
    Manutencao.objects.create(
        veiculo=veiculo,
        tipo="corretiva",
        data=date(2026, 7, 10),
        descricao="Farol",
        custo_real=Decimal("300.00"),
    )
    opcoes = _params(views._opcoes_de_mes(2026, 10, date(2026, 10, 27)))
    assert opcoes == ["2026-10", "2026-09", "2026-08", "2026-07"]


def test_ir_para_inclui_mes_de_cobranca_antiga(cliente):
    Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.ALUGUEL,
        descricao="Aluguel antigo",
        valor=Decimal("650.00"),
        vencimento=date(2026, 8, 3),
    )
    assert services.primeiro_mes_com_dado() == date(2026, 8, 3)
    assert _params(views._opcoes_de_mes(2026, 10, date(2026, 10, 27)))[-1] == "2026-08"


def test_ir_para_inclui_mes_de_movimento_de_caucao(cliente, veiculo):
    alocacao = Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 9, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=10_000,
    )
    caucao = Caucao.objects.create(alocacao=alocacao)
    MovimentacaoCaucao.objects.create(
        caucao=caucao,
        tipo=MovimentacaoCaucao.Tipo.REFORCO,
        valor=Decimal("100.00"),
        data=date(2026, 6, 15),
    )
    assert services.primeiro_mes_com_dado() == date(2026, 6, 15)


def test_relatorios_nao_reexporta_helper_do_financeiro():
    # o import sem uso (escondido com noqa) saiu: quem usa é o financeiro
    assert not hasattr(services, "entradas_fora_do_das")


def test_planilha_recebiveis_credito_maior_que_divida_nao_fica_negativa(cliente):
    hoje = timezone.localdate()
    Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.ALUGUEL,
        descricao="Aluguel",
        valor=Decimal("100.00"),
        vencimento=hoje - timedelta(days=1),
    )
    MovimentoCredito.objects.create(
        cliente=cliente, tipo=MovimentoCredito.Tipo.ENTRADA, valor=Decimal("300.00"), data=hoje
    )
    _, colunas, linhas = views._planilha_recebiveis(hoje.year, hoje.month)
    saldo = colunas.index("Saldo devedor (R$)")
    # soma do cliente = o "falta" da tela (zero: o crédito já cobre tudo)
    assert sum(linha[saldo] for linha in linhas) == 0
    [credito] = [linha for linha in linhas if "Crédito guardado" in linha[1]]
    assert credito[saldo] == -100.0
    assert "R$ 300,00" in credito[1] and "sobram R$ 200,00" in credito[1]


def test_planilha_recebiveis_credito_menor_que_divida_abate_tudo(cliente):
    hoje = timezone.localdate()
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
    _, colunas, linhas = views._planilha_recebiveis(hoje.year, hoje.month)
    saldo = colunas.index("Saldo devedor (R$)")
    assert sum(linha[saldo] for linha in linhas) == 600.0
    [credito] = [linha for linha in linhas if "Crédito guardado" in linha[1]]
    assert credito[1] == "Crédito guardado do cliente (abate do que deve)"


def test_setas_de_mes_iguais_as_do_das(usuario_logado):
    hoje = timezone.localdate()
    html = usuario_logado.get("/relatorios/", {"mes": "2000-01"}).content.decode()
    assert "Relatórios — Janeiro de 2000" in html
    # antes de 2000 não há dado: sem seta para trás (antes ia para ?mes=1999-12)
    assert "mês anterior" not in html
    assert "próximo mês" in html
    assert "voltar para o mês de hoje" in html

    html = usuario_logado.get("/relatorios/").content.decode()
    assert "mês anterior" in html
    assert "próximo mês" not in html
    assert str(hoje.year) in html


def _abas(conteudo):
    return load_workbook(io.BytesIO(conteudo))


def _linhas(aba):
    return [list(linha) for linha in aba.iter_rows(values_only=True)]


def test_abas_do_das_iguais_em_relatorios_e_financeiro(usuario_logado, cliente, veiculo):
    alocacao = Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 9, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=10_000,
    )
    caucao = Caucao.objects.create(alocacao=alocacao)
    MovimentacaoCaucao.objects.create(
        caucao=caucao,
        tipo=MovimentacaoCaucao.Tipo.RECEBIMENTO,
        valor=Decimal("500.00"),
        data=date(2026, 9, 1),
    )
    relatorio = _abas(
        usuario_logado.get("/relatorios/", {"mes": "2026-09", "exportar": "tudo"}).content
    )
    das = _abas(
        usuario_logado.get("/financeiro/das/", {"mes": "2026-09", "exportar": "xlsx"}).content
    )
    assert _linhas(relatorio["DAS"])[1:] == _linhas(das.worksheets[0])[1:]
    pagamentos = _linhas(relatorio["Pagamentos do DAS"])
    assert pagamentos[1:] == _linhas(das.worksheets[1])[1:]
    # a caução recebida aparece nas duas, fora do DAS
    assert any(500.0 in linha for linha in pagamentos)


def test_ir_para_ignora_carro_pessoal_antigo(veiculo):
    # carro pessoal dos donos não entra no relatório: a compra de 2015 e a
    # manutenção dele não abrem 10 anos de meses vazios na caixinha "Ir para"
    pessoal = Veiculo.objects.create(
        placa="PES0A01",
        marca_modelo="Corolla",
        uso=Veiculo.Uso.FORA_LOCACAO,
        data_aquisicao=date(2015, 3, 1),
    )
    Manutencao.objects.create(
        veiculo=pessoal,
        tipo="corretiva",
        data=date(2016, 5, 10),
        descricao="Pneu",
        custo_real=Decimal("400.00"),
    )
    Manutencao.objects.create(
        veiculo=veiculo,
        tipo="corretiva",
        data=date(2026, 9, 10),
        descricao="Farol",
        custo_real=Decimal("300.00"),
    )
    assert services.primeiro_mes_com_dado() == date(2026, 9, 10)
    assert _params(views._opcoes_de_mes(2026, 10, date(2026, 10, 27))) == ["2026-10", "2026-09"]


def test_botao_baixar_tudo_cita_a_aba_de_pagamentos_do_das(usuario_logado):
    html = usuario_logado.get("/relatorios/").content.decode()
    assert "frota e os pagamentos do DAS" in html
