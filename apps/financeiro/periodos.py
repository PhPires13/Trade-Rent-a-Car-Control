"""Leitura do parâmetro ?mes=AAAA-MM das telas mensais (DAS e relatórios)."""

from datetime import date


def ano_mes(request):
    """(ano, mês) do parâmetro ?mes=AAAA-MM; entrada inválida cai no mês atual.

    Valida o mês de verdade (mes=2026-13 dava erro 500 nas agregações) —
    para a Luciana, um link errado deve mostrar o mês atual, não quebrar.
    """
    try:
        ano, mes = request.GET.get("mes", "").split("-")
        ano, mes = int(ano), int(mes)
        date(ano, mes, 1)  # rejeita mês fora de 1–12 e ano implausível
        return ano, mes
    except (ValueError, AttributeError):
        hoje = date.today()
        return hoje.year, hoje.month
