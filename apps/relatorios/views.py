from datetime import date

from django.shortcuts import render

from . import exportar, services


def _ano_mes(request):
    try:
        ano, mes = request.GET.get("mes", "").split("-")
        return int(ano), int(mes)
    except (ValueError, AttributeError):
        hoje = date.today()
        return hoje.year, hoje.month


def relatorios(request):
    """Central de relatórios mensais com exportação p/ contabilidade (docs.md §5)."""
    ano, mes = _ano_mes(request)
    tipo = request.GET.get("exportar")
    formato = request.GET.get("formato", "xlsx")
    if tipo:
        return _exportar(tipo, formato, ano, mes)
    contexto = {
        "ano": ano,
        "mes": mes,
        "mes_str": f"{ano}-{mes:02d}",
        "receitas": services.receitas_do_mes(ano, mes),
        "despesas": services.despesas_do_mes(ano, mes),
        "recebiveis": services.recebiveis_em_aberto(),
        "caucoes": services.caucoes_retidas(),
    }
    contexto["fichas"], contexto["media_frota"] = services.resumo_da_frota()
    return render(request, "relatorios/relatorios.html", contexto)


def _exportar(tipo, formato, ano, mes):
    sufixo = f"{ano}-{mes:02d}"
    if tipo == "receitas":
        r = services.receitas_do_mes(ano, mes)
        linhas = [["Receita de locação (fatura) — BASE DO DAS", float(r["locacao"]), "Sim"]]
        for rotulo, valor in r["diversos"].items():
            linhas.append([f"Pagamentos diversos — {rotulo} (ND)", float(valor), "Não"])
        linhas.append(["Caução recebida", float(r["caucao_recebida"]), "Não"])
        linhas.append(["Auxílio motorista recebido", float(r["total_auxilios"]), "Não"])
        linhas.append(["Venda de veículos", float(r["total_vendas"]), "Não"])
        return exportar.exportar(
            formato,
            f"receitas-{sufixo}",
            "Receitas",
            ["Grupo", "Valor (R$)", "Base do DAS?"],
            linhas,
        )
    if tipo == "despesas":
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
        linhas.append(["", "", "TOTAL", "", float(d["total_geral"])])
        return exportar.exportar(
            formato,
            f"despesas-{sufixo}",
            "Despesas",
            ["Data", "Veículo", "Origem", "Descrição", "Valor (R$)"],
            linhas,
        )
    if tipo == "recebiveis":
        linhas = []
        for registro in services.recebiveis_em_aberto():
            for cobranca in registro["cobrancas"]:
                linhas.append(
                    [
                        registro["cliente"].nome,
                        cobranca.descricao,
                        cobranca.vencimento.strftime("%d/%m/%Y"),
                        cobranca.get_status_display(),
                        float(cobranca.saldo),
                    ]
                )
        return exportar.exportar(
            formato,
            f"recebiveis-{sufixo}",
            "Recebíveis",
            ["Cliente", "Cobrança", "Vencimento", "Status", "Saldo devedor (R$)"],
            linhas,
        )
    # frota
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
    return exportar.exportar(
        formato,
        f"frota-{sufixo}",
        "Frota",
        [
            "Placa",
            "Modelo",
            "Investido",
            "Receita",
            "Despesas",
            "Resultado",
            "% recuperado",
            "Recomendação",
        ],
        linhas,
    )
