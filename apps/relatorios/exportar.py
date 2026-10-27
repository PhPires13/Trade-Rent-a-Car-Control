"""Exportação Excel/CSV para envio à contabilidade (decisão nº 18)."""

import csv
from decimal import Decimal
from io import BytesIO

from django.http import HttpResponse
from openpyxl import Workbook
from openpyxl.styles import Font

#: começo de texto que o Excel lê como fórmula ("=HIPERLINK(...)", "+1", "@SOMA")
INICIO_DE_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def texto_seguro(valor):
    """CSV: texto livre que começa como fórmula ganha um apóstrofo — o Excel mostra como texto.

    Descrição de manutenção/multa é digitada à mão; "=troca de óleo" virava
    #NOME? (ou um link ativo) na planilha que vai para o contador.
    """
    if isinstance(valor, str) and valor.startswith(INICIO_DE_FORMULA):
        return "'" + valor
    return valor


def _preencher_aba(ws, titulo, colunas, linhas):
    ws.title = titulo[:31]
    ws.append(colunas)
    for celula in ws[1]:
        celula.font = Font(bold=True)
    for linha in linhas:
        ws.append(list(linha))
        for celula in ws[ws.max_row]:
            if isinstance(celula.value, str):
                # nunca grava texto como fórmula: o Excel mostra "=troca de óleo" como texto
                celula.data_type = "s"
            elif isinstance(celula.value, (float, Decimal)):
                # valores com centavos e milhar: 1.234,50 no Excel em português
                celula.number_format = "#,##0.00"
    for indice, coluna in enumerate(colunas, 1):
        largura = max(
            [len(str(coluna))]
            + [
                len(str(linha[indice - 1]))
                for linha in linhas
                if len(linha) >= indice and linha[indice - 1] is not None
            ]
        )
        ws.column_dimensions[ws.cell(row=1, column=indice).column_letter].width = min(
            largura + 2, 60
        )


def _resposta_xlsx(wb, nome_arquivo):
    buffer = BytesIO()
    wb.save(buffer)
    resposta = HttpResponse(
        buffer.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resposta["Content-Disposition"] = f'attachment; filename="{nome_arquivo}.xlsx"'
    return resposta


def planilha_xlsx(nome_arquivo, titulo, colunas, linhas):
    wb = Workbook()
    _preencher_aba(wb.active, titulo, colunas, linhas)
    return _resposta_xlsx(wb, nome_arquivo)


def planilha_xlsx_abas(nome_arquivo, abas):
    """Um arquivo só com várias abas: [(título, colunas, linhas), ...]."""
    wb = Workbook()
    for posicao, (titulo, colunas, linhas) in enumerate(abas):
        ws = wb.active if posicao == 0 else wb.create_sheet()
        _preencher_aba(ws, titulo, colunas, linhas)
    return _resposta_xlsx(wb, nome_arquivo)


def _valor_csv(valor):
    """Número no jeito brasileiro (1234,50): o Excel em português lê como número."""
    if isinstance(valor, (float, Decimal)):
        return f"{Decimal(str(valor)):.2f}".replace(".", ",")
    return texto_seguro(valor)


def planilha_csv(nome_arquivo, colunas, linhas):
    """CSV para o Excel em português: BOM (acentos certos), ";" entre colunas e
    vírgula nos centavos — o mesmo formato do CSV do DAS."""
    resposta = HttpResponse(content_type="text/csv")
    resposta["Content-Disposition"] = f'attachment; filename="{nome_arquivo}.csv"'
    resposta.write("\ufeff")
    writer = csv.writer(resposta, delimiter=";")
    writer.writerow(colunas)
    writer.writerows([_valor_csv(valor) for valor in linha] for linha in linhas)
    return resposta


def exportar(formato, nome_arquivo, titulo, colunas, linhas):
    if formato == "csv":
        return planilha_csv(nome_arquivo, colunas, linhas)
    # qualquer outro valor (ou nenhum): a planilha do Excel, que é o padrão
    return planilha_xlsx(nome_arquivo, titulo, colunas, linhas)
