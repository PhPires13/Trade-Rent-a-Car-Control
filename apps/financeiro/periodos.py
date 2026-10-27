"""Leitura do parâmetro ?mes=AAAA-MM das telas mensais (DAS e relatórios).

Também as setas de mês ("◀ mês anterior / próximo mês ▶") da tela do DAS —
funcionam em qualquer navegador, ao contrário do <input type=month>, que o
Safari não mostra.
"""

from datetime import date

from django.utils import timezone

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
#: antes disso não há dado; ano absurdo na URL cai no mês padrão
ANO_MINIMO = 2000
#: o DAS do mês vence no dia 20 do mês seguinte: até lá a tela abre no mês passado
DIA_PRAZO_DAS = 20


def ano_mes(request, padrao=None):
    """(ano, mês) do parâmetro ?mes=AAAA-MM; entrada inválida cai no mês padrão.

    Valida o mês de verdade (mes=2026-13 dava erro 500 nas agregações) —
    para a Luciana, um link errado deve mostrar o mês atual, não quebrar.
    `padrao`: (ano, mês) usado sem ?mes= ou com valor inválido (mês atual se None).
    """
    try:
        ano, mes = request.GET.get("mes", "").split("-")
        ano, mes = int(ano), int(mes)
        date(ano, mes, 1)  # rejeita mês fora de 1–12 e ano implausível
        return ano, mes
    except (ValueError, AttributeError):
        if padrao is not None:
            return padrao
        hoje = timezone.localdate()
        return hoje.year, hoje.month


def vizinho(ano, mes, passo):
    """(ano, mês) um mês antes (passo=-1) ou depois (passo=1), virando o ano."""
    indice = ano * 12 + (mes - 1) + passo
    return indice // 12, indice % 12 + 1


def mes_para_tela(ano, mes):
    """Link ?mes= e nome por extenso ("Outubro" de 2026)."""
    return {"param": f"{ano}-{mes:02d}", "nome": MESES[mes - 1], "ano": ano}


def nome_do_mes(ano, mes):
    return f"{MESES[mes - 1]} de {ano}"


def mes_padrao_do_das(hoje=None):
    """Mês que a tela do DAS abre sem ?mes=: o anterior até o dia 20 (prazo do DAS).

    No começo do mês, o mês corrente está quase vazio — quem abre a tela nessa
    época quer o número do mês fechado, que vai para a contabilidade.
    """
    hoje = hoje or timezone.localdate()
    if hoje.day <= DIA_PRAZO_DAS:
        return vizinho(hoje.year, hoje.month, -1)
    return hoje.year, hoje.month


def navegacao_de_meses(ano, mes, hoje=None):
    """Contexto das setas: mês anterior, seguinte (nunca além do mês de hoje) e "voltar a hoje"."""
    hoje = hoje or timezone.localdate()
    atual = (hoje.year, hoje.month)
    return {
        "nome_mes": nome_do_mes(ano, mes),
        "mes_anterior": (
            mes_para_tela(*vizinho(ano, mes, -1)) if (ano, mes) > (ANO_MINIMO, 1) else None
        ),
        # mês que ainda não começou não tem o que mostrar — sem seta pra frente
        "mes_seguinte": mes_para_tela(*vizinho(ano, mes, 1)) if (ano, mes) < atual else None,
        "mes_atual": None if (ano, mes) == atual else mes_para_tela(*atual),
    }
