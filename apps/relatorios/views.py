from django.shortcuts import render

from apps.financeiro.periodos import ano_mes

from . import exportar, services


def relatorios(request):
    """Central de relatórios mensais com exportação p/ contabilidade (docs.md §5)."""
    ano, mes = ano_mes(request)
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
        "serie_mensal": services.serie_mensal(ano, mes),
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
