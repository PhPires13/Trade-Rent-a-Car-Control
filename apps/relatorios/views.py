from django.shortcuts import render
from django.utils import timezone

from apps.financeiro.periodos import ano_mes
from apps.financeiro.services import pagamentos_do_mes, resumo_fiscal
from apps.financeiro.views import COLUNAS_PAGAMENTOS, _linhas_pagamentos

from . import exportar, services

MESES = (
    "Janeiro",
    "Fevereiro",
    "Março",
    "Abril",
    "Maio",
    "Junho",
    "Julho",
    "Agosto",
    "Setembro",
    "Outubro",
    "Novembro",
    "Dezembro",
)
#: nível do ranking de venda (apps.frota.desmobilizacao.NIVEIS) em português
RECOMENDACOES = {
    "manter": "Manter",
    "observar": "Observar",
    "preparar": "Preparar venda",
    "vender": "Vender",
}
ANO_MINIMO = 2000  # antes disso não há dado; ano absurdo na URL cai no mês atual


def _vizinho(ano, mes, passo):
    """(ano, mês) um mês antes (passo=-1) ou depois (passo=1), virando o ano."""
    indice = ano * 12 + (mes - 1) + passo
    return indice // 12, indice % 12 + 1


def _mes_para_tela(ano, mes):
    """Link ?mes= e nome por extenso ("Outubro de 2026") — as setas de mês
    funcionam em qualquer navegador, ao contrário do <input type=month>."""
    return {"param": f"{ano}-{mes:02d}", "nome": MESES[mes - 1], "ano": ano}


def _mes_pedido(request, hoje):
    """(ano, mês, aviso): mês inválido, absurdo ou que ainda não começou cai no
    mês atual — e a tela avisa, em vez de trocar de mês calada."""
    pedido = request.GET.get("mes", "").strip()
    ano, mes = ano_mes(request)
    valido = pedido == f"{ano}-{mes:02d}" or pedido == f"{ano}-{mes}"
    if not valido or not ANO_MINIMO <= ano or (ano, mes) > (hoje.year, hoje.month):
        # ?mes=0001-01 derrubava o gráfico (6 meses antes do ano 1 não existe)
        aviso = "Mês inválido — mostrando o mês atual." if pedido else None
        return hoje.year, hoje.month, aviso
    return ano, mes, None


def _opcoes_de_mes(ano, mes, hoje, quantidade=60):
    """Meses da caixinha de escolha: do mês atual para trás (5 anos)."""
    opcoes = []
    a, m = hoje.year, hoje.month
    for _ in range(quantidade):
        opcoes.append({"param": f"{a}-{m:02d}", "nome": f"{MESES[m - 1]} de {a}"})
        a, m = _vizinho(a, m, -1)
    if not any(o["param"] == f"{ano}-{mes:02d}" for o in opcoes):
        opcoes.append({"param": f"{ano}-{mes:02d}", "nome": f"{MESES[mes - 1]} de {ano}"})
    return opcoes


def relatorios(request):
    """Central de relatórios mensais com exportação p/ contabilidade (docs.md §5)."""
    hoje = timezone.localdate()  # mesmo "hoje" do ano_mes (fuso de BH)
    ano, mes, aviso_mes = _mes_pedido(request, hoje)
    tipo = request.GET.get("exportar")
    formato = request.GET.get("formato", "xlsx")
    if tipo:
        return _exportar(tipo, formato, ano, mes, hoje)
    receitas = services.receitas_do_mes(ano, mes)
    despesas = services.despesas_do_mes(ano, mes)
    contexto = {
        "ano": ano,
        "mes": mes,
        "mes_str": f"{ano}-{mes:02d}",
        "nome_mes": f"{MESES[mes - 1]} de {ano}",
        "hoje": hoje,
        "aviso_mes": aviso_mes,
        "opcoes_de_mes": _opcoes_de_mes(ano, mes, hoje),
        "mes_anterior": _mes_para_tela(*_vizinho(ano, mes, -1)),
        # mês que ainda não começou não tem o que mostrar — sem seta pra frente
        "mes_seguinte": (
            _mes_para_tela(*_vizinho(ano, mes, 1)) if (ano, mes) < (hoje.year, hoje.month) else None
        ),
        "mes_atual": None
        if (ano, mes) == (hoje.year, hoje.month)
        else _mes_para_tela(hoje.year, hoje.month),
        "receitas": receitas,
        "despesas": despesas,
        "resumo": services.resumo_do_mes(receitas, despesas),
        "recebiveis": services.recebiveis_em_aberto(),
        "caucoes": services.caucoes_retidas(),
        "serie_mensal": services.serie_mensal(ano, mes),
    }
    contexto["fichas"], contexto["media_frota"] = services.resumo_da_frota()
    return render(request, "relatorios/relatorios.html", contexto)


def _exportar(tipo, formato, ano, mes, hoje):
    """Planilha de um dos quadros; tipo desconhecido cai no resumo da frota.

    "tudo": um .xlsx só com todas as abas — o que vai para a contabilidade.
    Recebíveis e frota são a situação de HOJE: o nome do arquivo leva a data
    de hoje, não o mês escolhido, para ninguém achar que é foto do passado.
    """
    if tipo == "tudo":
        abas = [
            _PLANILHAS["receitas"](ano, mes),
            _PLANILHAS["despesas"](ano, mes),
            _planilha_das(ano, mes),
            _PLANILHAS["recebiveis"](ano, mes),
            _PLANILHAS["frota"](ano, mes),
            # cada pagamento que compõe o DAS (a 2ª aba do Financeiro › DAS)
            _planilha_pagamentos_das(ano, mes),
        ]
        return exportar.planilha_xlsx_abas(f"contabilidade-{ano}-{mes:02d}", abas)
    if tipo not in _PLANILHAS:
        tipo = "frota"
    titulo, colunas, linhas = _PLANILHAS[tipo](ano, mes)
    sufixo = hoje.isoformat() if tipo in _DE_HOJE else f"{ano}-{mes:02d}"
    return exportar.exportar(formato, f"{tipo}-{sufixo}", titulo, colunas, linhas)


def _planilha_das(ano, mes):
    """Aba do DAS — as mesmas linhas da planilha de Financeiro › DAS."""
    r = resumo_fiscal(ano, mes)
    linhas = [["Receita de locação (fatura)", r["locacao"], "Sim"]]
    for rotulo, valor in r["diversos"].items():
        linhas.append([f"Pagamentos diversos — {rotulo} (ND)", valor, "Não"])
    linhas.append(["Caução recebida (não é receita — é do cliente)", r["caucao_recebida"], "Não"])
    linhas.append(["Adiantamentos recebidos (crédito do cliente)", r["adiantamentos"], "Não"])
    linhas.append(["Pagos com crédito do cliente (já contados acima)", r["pago_com_credito"], "-"])
    linhas.append(["Total que entrou no caixa", r["entrou_no_caixa"], "-"])
    return "DAS", ["Grupo", "Valor (R$)", "Entra na base do DAS?"], linhas


def _planilha_pagamentos_das(ano, mes):
    """Cada pagamento que compõe o DAS — a mesma 2ª aba de Financeiro › DAS."""
    return "Pagamentos do DAS", COLUNAS_PAGAMENTOS, _linhas_pagamentos(pagamentos_do_mes(ano, mes))


def _planilha_receitas(ano, mes):
    r = services.receitas_do_mes(ano, mes)
    linhas = [["Receita de locação (fatura) — BASE DO DAS", float(r["locacao"]), "Sim"]]
    for rotulo, valor in r["diversos"].items():
        linhas.append([f"Pagamentos diversos — {rotulo} (ND)", float(valor), "Não"])
    linhas.append(["Auxílio motorista recebido", float(r["total_auxilios"]), "Não"])
    linhas.append(["Venda de veículos", float(r["total_vendas"]), "Não"])
    linhas.append(["TOTAL DAS RECEITAS", float(r["total_receitas"]), ""])
    # caução depois do total: é dinheiro do cliente, não receita
    linhas.append(
        ["Caução recebida (não é receita — é do cliente)", float(r["caucao_recebida"]), "Não"]
    )
    # sobra que virou crédito: fora do total; entra na base quando o crédito é usado
    linhas.append(
        [
            "Adiantamentos recebidos (crédito do cliente — entra quando for usado)",
            float(r["adiantamentos"]),
            "Não",
        ]
    )
    return "Receitas", ["Grupo", "Valor (R$)", "Base do DAS?"], linhas


def _planilha_despesas(ano, mes):
    d = services.despesas_do_mes(ano, mes)
    linhas = [
        [
            m.data.strftime("%d/%m/%Y"),
            m.veiculo.placa,
            "Manutenção",
            m.descricao[:80],
            float(m.custo_real),
        ]
        for m in d["manutencoes"]
    ]
    linhas += [
        [
            s.data_ref.strftime("%d/%m/%Y"),
            s.veiculo.placa,
            "Franquia de evento",
            s.get_tipo_display(),
            float(s.franquia_valor),
        ]
        for s in d["franquias"]
    ]
    linhas += [
        ["", v.placa, "Proteção Auto Truck (mensalidade)", "", float(v.mensalidade_protecao)]
        for v in d["frota_protegida"]
    ]
    linhas += [
        [
            m.data_infracao.strftime("%d/%m/%Y"),
            m.veiculo.placa,
            "Multa absorvida pela empresa",
            (m.descricao or m.codigo)[:80],
            float(m.valor),
        ]
        for m in d["multas_empresa"]
    ]
    linhas += [
        [
            v.data_aquisicao.strftime("%d/%m/%Y"),
            v.placa,
            "Custos de compra",
            v.marca_modelo[:80],
            float(v.custos_entrada),
        ]
        for v in d["compras"]
    ]
    linhas += [
        [
            p.pago_em.strftime("%d/%m/%Y"),
            p.veiculo.placa,
            "IPVA",
            p.rotulo,
            float(p.valor),
        ]
        for p in d["ipvas"]
    ]
    linhas += [
        [
            v.data_venda.strftime("%d/%m/%Y"),
            v.placa,
            "Custos de venda",
            v.marca_modelo[:80],
            float(v.custos_venda),
        ]
        for v in d["vendas"]
    ]
    # toda linha acima soma no TOTAL e vice-versa — a planilha fecha com ela mesma
    linhas.append(["", "", "TOTAL", "", float(d["total_geral"])])
    return "Despesas", ["Data", "Veículo", "Origem", "Descrição", "Valor (R$)"], linhas


def _planilha_recebiveis(ano, mes):
    """Saldo devedor de hoje (não do mês escolhido)."""
    linhas = [
        [
            registro["cliente"].nome,
            cobranca.descricao,
            cobranca.vencimento.strftime("%d/%m/%Y"),
            cobranca.get_status_display(),
            float(cobranca.saldo),
        ]
        for registro in services.recebiveis_em_aberto()
        for cobranca in registro["cobrancas"]
    ]
    colunas = ["Cliente", "Cobrança", "Vencimento", "Status", "Saldo devedor (R$)"]
    return f"Recebíveis hoje {timezone.localdate():%d-%m-%Y}", colunas, linhas


def _planilha_frota(ano, mes):
    """Ranking de hoje (não do mês escolhido)."""
    fichas, _ = services.resumo_da_frota()
    linhas = [
        [
            f.veiculo.placa,
            f.veiculo.marca_modelo,
            float(f.investido),
            float(f.receita_total),
            float(f.despesa_total),
            float(f.resultado_operacional),
            # 0–100, como na tela ("Já voltou 75%"); sem investimento: vazio, não 0
            (
                round(float(f.percentual_recuperado) * 100, 1)
                if f.percentual_recuperado is not None
                else None
            ),
            RECOMENDACOES.get(f.nivel, f.nivel),
        ]
        for f in fichas
    ]
    colunas = [
        "Placa",
        "Modelo",
        "Investido",
        "Receita",
        "Despesas",
        "Resultado",
        "% recuperado (0–100)",
        "Recomendação",
    ]
    return f"Frota hoje {timezone.localdate():%d-%m-%Y}", colunas, linhas


_PLANILHAS = {
    "receitas": _planilha_receitas,
    "despesas": _planilha_despesas,
    "recebiveis": _planilha_recebiveis,
    "frota": _planilha_frota,
}

#: quadros que mostram a situação de hoje, não a do mês escolhido
_DE_HOJE = {"recebiveis", "frota"}
