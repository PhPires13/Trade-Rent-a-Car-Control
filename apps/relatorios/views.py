from datetime import date

from django.shortcuts import render

from apps.financeiro.periodos import ano_mes

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
ANO_MINIMO = 2000  # antes disso não há dado; ano absurdo na URL cai no mês atual


def _vizinho(ano, mes, passo):
    """(ano, mês) um mês antes (passo=-1) ou depois (passo=1), virando o ano."""
    indice = ano * 12 + (mes - 1) + passo
    return indice // 12, indice % 12 + 1


def _mes_para_tela(ano, mes):
    """Link ?mes= e nome por extenso ("Outubro de 2026") — as setas de mês
    funcionam em qualquer navegador, ao contrário do <input type=month>."""
    return {"param": f"{ano}-{mes:02d}", "nome": MESES[mes - 1], "ano": ano}


def relatorios(request):
    """Central de relatórios mensais com exportação p/ contabilidade (docs.md §5)."""
    ano, mes = ano_mes(request)
    hoje = date.today()
    if not ANO_MINIMO <= ano <= hoje.year + 10:
        # ?mes=0001-01 derrubava o gráfico (6 meses antes do ano 1 não existe)
        ano, mes = hoje.year, hoje.month
    tipo = request.GET.get("exportar")
    formato = request.GET.get("formato", "xlsx")
    if tipo:
        return _exportar(tipo, formato, ano, mes)
    receitas = services.receitas_do_mes(ano, mes)
    despesas = services.despesas_do_mes(ano, mes)
    contexto = {
        "ano": ano,
        "mes": mes,
        "mes_str": f"{ano}-{mes:02d}",
        "nome_mes": f"{MESES[mes - 1]} de {ano}",
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


def _exportar(tipo, formato, ano, mes):
    """Planilha de um dos quadros; tipo desconhecido cai no resumo da frota."""
    if tipo not in _PLANILHAS:
        tipo = "frota"
    titulo, colunas, linhas = _PLANILHAS[tipo](ano, mes)
    return exportar.exportar(formato, f"{tipo}-{ano}-{mes:02d}", titulo, colunas, linhas)


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
            s.data_evento.strftime("%d/%m/%Y"),
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
    """Saldo devedor de hoje — o mês só entra no nome do arquivo."""
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
    return "Recebíveis", colunas, linhas


def _planilha_frota(ano, mes):
    """Ranking de hoje — o mês só entra no nome do arquivo."""
    fichas, _ = services.resumo_da_frota()
    linhas = [
        [
            f.veiculo.placa,
            f.veiculo.marca_modelo,
            float(f.investido),
            float(f.receita_total),
            float(f.despesa_total),
            float(f.resultado_operacional),
            float(f.percentual_recuperado or 0),
            f.nivel,
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
        "% recuperado",
        "Recomendação",
    ]
    return "Frota", colunas, linhas


_PLANILHAS = {
    "receitas": _planilha_receitas,
    "despesas": _planilha_despesas,
    "recebiveis": _planilha_recebiveis,
    "frota": _planilha_frota,
}
