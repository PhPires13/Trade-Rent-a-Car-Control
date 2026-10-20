"""Revisão de UX dos relatórios (proposta #12, opção A): setas de mês com nome
por extenso, "Quanto sobrou este mês", total nas receitas e caução à parte."""

import csv
import io
from datetime import date
from decimal import Decimal

import pytest

from apps.alocacoes.models import Alocacao
from apps.financeiro import services as financeiro
from apps.financeiro.models import Cobranca
from apps.frota.models import Veiculo
from apps.manutencao.models import Manutencao
from apps.pessoas.models import Cliente
from apps.relatorios import services, views
from apps.sinistros.models import AuxilioMotorista, Sinistro


@pytest.fixture
def cenario(db):
    """Julho/2026: aluguel 650 + auxílio 1.518 entram; caução 1.000 é do cliente;
    manutenção 400 + proteção 304 saem."""
    veiculo = Veiculo.objects.create(
        placa="QXQ6C10",
        marca_modelo="Gol",
        mensalidade_protecao=Decimal("304.00"),
        data_aquisicao=date(2025, 1, 1),
    )
    cliente = Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")
    alocacao = Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
    )
    financeiro.abrir_caucao(alocacao, valor_recebido="1000.00", data=date(2026, 7, 1))
    cobranca = Cobranca.objects.create(
        cliente=cliente,
        alocacao=alocacao,
        origem="aluguel",
        descricao="Semana 1",
        valor=Decimal("650.00"),
        vencimento=date(2026, 7, 8),
    )
    financeiro.registrar_recebimento(
        cliente, date(2026, 7, 8), Decimal("650.00"), "pix", [(cobranca, Decimal("650.00"))]
    )
    sinistro = Sinistro.objects.create(veiculo=veiculo, data=date(2026, 7, 1), envolvido="terceiro")
    AuxilioMotorista.objects.create(
        sinistro=sinistro,
        valor=Decimal("1518.00"),
        status="recebido",
        data_recebimento=date(2026, 7, 15),
    )
    Manutencao.objects.create(
        veiculo=veiculo,
        tipo="corretiva",
        data=date(2026, 7, 10),
        descricao="Freios",
        custo_real=Decimal("400.00"),
    )
    return veiculo


# ---------- contas ----------


def test_total_das_receitas_deixa_a_caucao_de_fora(cenario):
    receitas = services.receitas_do_mes(2026, 7)
    assert receitas["caucao_recebida"] == Decimal("1000.00")
    assert receitas["total_receitas"] == Decimal("2168.00")  # 650 + 1.518


def test_quanto_sobrou_e_receitas_menos_despesas(cenario):
    resumo = services.resumo_do_mes(
        services.receitas_do_mes(2026, 7), services.despesas_do_mes(2026, 7)
    )
    assert resumo["entrou"] == Decimal("2168.00")
    assert resumo["saiu"] == Decimal("704.00")
    assert resumo["sobrou"] == Decimal("1464.00")
    assert resumo["base_das"] == Decimal("650.00")


def test_total_bate_com_a_barra_do_grafico(cenario):
    serie = services.serie_mensal(2026, 7)
    assert serie[-1]["receita"] == float(services.receitas_do_mes(2026, 7)["total_receitas"])


# ---------- navegação de mês ----------


@pytest.mark.parametrize(
    "ano,mes,passo,esperado",
    [(2026, 10, -1, (2026, 9)), (2026, 1, -1, (2025, 12)), (2026, 12, 1, (2027, 1))],
)
def test_mes_vizinho_vira_o_ano(ano, mes, passo, esperado):
    assert views._vizinho(ano, mes, passo) == esperado


def test_titulo_com_nome_do_mes_e_setas(usuario_logado, cenario):
    html = usuario_logado.get("/relatorios/?mes=2026-07").content.decode()
    assert "Julho de 2026" in html
    assert 'href="?mes=2026-06"' in html and "mês anterior" in html
    assert 'href="?mes=2026-08"' in html and "próximo mês" in html
    assert 'type="month"' not in html  # vira caixa de texto no Safari/Firefox


def test_mes_atual_nao_tem_seta_pra_frente(usuario_logado, db):
    hoje = date.today()
    html = usuario_logado.get(f"/relatorios/?mes={hoje:%Y-%m}").content.decode()
    assert "próximo mês" not in html
    assert "mês anterior" in html
    assert "voltar para o mês de hoje" not in html


def test_mes_passado_oferece_voltar_para_hoje(usuario_logado, db):
    html = usuario_logado.get("/relatorios/?mes=2020-03").content.decode()
    assert "Março de 2020" in html
    assert "voltar para o mês de hoje" in html


# ---------- tela ----------


def test_bloco_quanto_sobrou_na_tela(usuario_logado, cenario):
    html = usuario_logado.get("/relatorios/?mes=2026-07").content.decode()
    assert "Quanto sobrou este mês" in html
    assert 'data-sobrou="positivo"' in html
    assert "R$ 1.464,00" in html  # sobrou
    assert "R$ 2.168,00" in html  # entrou = total das receitas
    assert "não é receita (é do cliente)" in html
    assert "Regime de caixa" not in html  # explicado em palavras simples


def test_mes_no_vermelho_mostra_faltou(usuario_logado, db):
    Veiculo.objects.create(
        placa="RNB9J66",
        marca_modelo="Voyage",
        mensalidade_protecao=Decimal("304.00"),
        data_aquisicao=date(2025, 1, 1),
    )
    html = usuario_logado.get("/relatorios/?mes=2026-07").content.decode()
    assert 'data-sobrou="negativo"' in html
    assert "Faltou" in html


def test_imprimir_esconde_menu_e_botoes(usuario_logado, db):
    html = usuario_logado.get("/relatorios/?mes=2026-07").content.decode()
    assert "window.print()" in html
    assert "@media print" in html and ".nao-imprime" in html


def test_frota_vira_cartoes_no_celular(usuario_logado, cenario):
    html = usuario_logado.get("/relatorios/?mes=2026-07").content.decode()
    assert '<ul class="divide-y divide-gray-100 sm:hidden">' in html
    assert "hidden overflow-x-auto sm:block" in html
    assert html.count("QXQ6C10") >= 2  # placa no cartão e na tabela


def test_planilha_de_receitas_tem_total_e_caucao_depois(usuario_logado, cenario):
    resposta = usuario_logado.get("/relatorios/?mes=2026-07&exportar=receitas&formato=csv")
    linhas = list(csv.reader(io.StringIO(resposta.content.decode("utf-8-sig"))))
    grupos = [linha[0] for linha in linhas]
    i_total = grupos.index("TOTAL DAS RECEITAS")
    i_caucao = next(i for i, g in enumerate(grupos) if g.startswith("Caução recebida"))
    assert i_caucao > i_total
    assert "2168" in linhas[i_total][1]


# ---------- revisão ----------


def test_faltou_mostra_valor_sem_sinal_de_menos(usuario_logado, db):
    Veiculo.objects.create(
        placa="RNB9J66",
        marca_modelo="Voyage",
        mensalidade_protecao=Decimal("304.00"),
        data_aquisicao=date(2025, 1, 1),
    )
    resposta = usuario_logado.get("/relatorios/?mes=2026-07")
    assert resposta.context["resumo"]["faltou"] == Decimal("304.00")
    html = resposta.content.decode()
    assert "-R$ 304,00" not in html  # "Faltou -R$ 304" seria sinal duplo


def test_mes_positivo_nao_tem_faltou(cenario):
    resumo = services.resumo_do_mes(
        services.receitas_do_mes(2026, 7), services.despesas_do_mes(2026, 7)
    )
    assert resumo["faltou"] == Decimal("0")


@pytest.mark.parametrize("mes", ["0001-01", "0001-03", "1999-12", "9999-12"])
def test_ano_absurdo_na_url_cai_no_mes_atual(usuario_logado, db, mes):
    hoje = date.today()
    resposta = usuario_logado.get(f"/relatorios/?mes={mes}")
    assert resposta.status_code == 200
    assert (resposta.context["ano"], resposta.context["mes"]) == (hoje.year, hoje.month)


def test_exportar_com_ano_absurdo_nao_quebra(usuario_logado, db):
    resposta = usuario_logado.get("/relatorios/?mes=0001-01&exportar=receitas&formato=csv")
    assert resposta.status_code == 200


def test_mes_futuro_sem_seta_pra_frente_mas_com_voltar(usuario_logado, db):
    hoje = date.today()
    html = usuario_logado.get(f"/relatorios/?mes={hoje.year + 1}-01").content.decode()
    assert "próximo mês" not in html
    assert "voltar para o mês de hoje" in html


def test_caucao_fora_da_tabela_de_receitas(usuario_logado, cenario):
    resposta = usuario_logado.get("/relatorios/?mes=2026-07")
    html = resposta.content.decode()
    tabela = html.split("Receitas do mês", 1)[1].split("</table>", 1)[0]
    assert "Caução" not in tabela
    assert "R$ 2.168,00" in tabela  # linha de Total
    assert resposta.context["resumo"]["entrou"] == Decimal("2168.00")


def test_planilha_total_soma_as_linhas_de_receita(usuario_logado, cenario):
    resposta = usuario_logado.get("/relatorios/?mes=2026-07&exportar=receitas&formato=csv")
    linhas = list(csv.reader(io.StringIO(resposta.content.decode("utf-8-sig"))))
    grupos = [linha[0] for linha in linhas]
    i_total = grupos.index("TOTAL DAS RECEITAS")

    def valor(texto):
        return Decimal(texto.replace(".", "").replace(",", ".")) if "," in texto else Decimal(texto)

    soma = sum((valor(linha[1]) for linha in linhas[1:i_total]), Decimal("0"))
    assert soma == valor(linhas[i_total][1]) == Decimal("2168")
