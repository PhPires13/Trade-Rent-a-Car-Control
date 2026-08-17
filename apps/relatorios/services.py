"""Agregações dos relatórios mensais para a contabilidade (docs.md §5, decisão nº 18)."""

from calendar import monthrange
from datetime import date

from django.db.models import Q, Sum

from apps.financeiro.models import (
    ZERO,
    AplicacaoRecebimento,
    Caucao,
    Cobranca,
    MovimentacaoCaucao,
)
from apps.financeiro.services import resumo_fiscal
from apps.frota.desmobilizacao import ranking_da_frota
from apps.frota.models import Veiculo
from apps.manutencao.models import Manutencao
from apps.multas.models import Multa
from apps.sinistros.models import AuxilioMotorista, Sinistro


def receitas_do_mes(ano, mes):
    """Créditos do mês nas três classes fiscais (decisão nº 11)."""
    fiscal = resumo_fiscal(ano, mes)
    auxilios = AuxilioMotorista.objects.filter(
        status="recebido", data_recebimento__year=ano, data_recebimento__month=mes
    ).select_related("sinistro__veiculo")
    vendas = Veiculo.objects.filter(data_venda__year=ano, data_venda__month=mes)
    total_auxilios = sum((a.valor or ZERO for a in auxilios), ZERO)
    total_vendas = sum((v.valor_venda or ZERO for v in vendas), ZERO)
    return {
        **fiscal,
        "auxilios": list(auxilios),
        "total_auxilios": total_auxilios,
        "vendas": list(vendas),
        "total_vendas": total_vendas,
        "total_outros_creditos": total_auxilios + total_vendas,
    }


def despesas_do_mes(ano, mes):
    """Despesas do mês por origem — pedido da Luciana para a contabilidade."""
    manutencoes = (
        Manutencao.objects.filter(data__year=ano, data__month=mes, custo_real__isnull=False)
        .select_related("veiculo", "oficina")
        .order_by("data")
    )
    total_manutencao = sum((m.custo_real for m in manutencoes), ZERO)
    # frota protegida DO MÊS: comprado até o fim dele e não vendido antes dele —
    # senão os meses passados mudariam a cada compra/venda de carro
    inicio_mes = date(ano, mes, 1)
    fim_mes = date(ano, mes, monthrange(ano, mes)[1])
    frota_protegida = (
        Veiculo.objects.filter(uso=Veiculo.Uso.LOCACAO, mensalidade_protecao__isnull=False)
        .filter(Q(data_aquisicao__isnull=True) | Q(data_aquisicao__lte=fim_mes))
        .exclude(data_venda__lt=inicio_mes)
        .exclude(data_venda__isnull=True, status=Veiculo.Status.VENDIDO)
    )
    total_protecao = sum((v.mensalidade_protecao for v in frota_protegida), ZERO)
    franquias = Sinistro.objects.filter(
        data_evento__year=ano, data_evento__month=mes, franquia_valor__isnull=False
    ).select_related("veiculo")
    total_franquias = sum((s.franquia_valor for s in franquias), ZERO)
    vendas = Veiculo.objects.filter(
        data_venda__year=ano, data_venda__month=mes, custos_venda__isnull=False
    )
    total_custos_venda = sum((v.custos_venda for v in vendas), ZERO)
    # multa absorvida pela empresa não gera cobrança — é despesa do mês (docs.md §5),
    # pela data da infração, como a ficha do veículo já conta (desmobilização)
    multas_empresa = (
        Multa.objects.filter(
            responsavel=Multa.Responsavel.EMPRESA,
            data_infracao__year=ano,
            data_infracao__month=mes,
            valor__isnull=False,
        )
        .select_related("veiculo")
        .order_by("data_infracao")
    )
    total_multas_empresa = sum((m.valor for m in multas_empresa), ZERO)
    compras = Veiculo.objects.filter(
        data_aquisicao__year=ano, data_aquisicao__month=mes, custos_entrada__isnull=False
    )
    total_custos_compra = sum((v.custos_entrada for v in compras), ZERO)
    return {
        "manutencoes": list(manutencoes),
        "total_manutencao": total_manutencao,
        "frota_protegida": list(frota_protegida),
        "total_protecao": total_protecao,
        "franquias": list(franquias),
        "total_franquias": total_franquias,
        "vendas": list(vendas),
        "total_custos_venda": total_custos_venda,
        "multas_empresa": list(multas_empresa),
        "total_multas_empresa": total_multas_empresa,
        "compras": list(compras),
        "total_custos_compra": total_custos_compra,
        "total_geral": (
            total_manutencao
            + total_protecao
            + total_franquias
            + total_custos_venda
            + total_multas_empresa
            + total_custos_compra
        ),
    }


def _meses_da_serie(ano, mes, quantidade):
    meses = []
    for _ in range(quantidade):
        meses.append((ano, mes))
        ano, mes = (ano - 1, 12) if mes == 1 else (ano, mes - 1)
    meses.reverse()
    return meses


def _somas_por_mes(queryset, campo, valor):
    """{(ano, mês): soma} em uma query só — a série não refaz consultas por mês."""
    pares = queryset.values_list(f"{campo}__year", f"{campo}__month").annotate(t=Sum(valor))
    return {(ano, mes): total or ZERO for ano, mes, total in pares}


def serie_mensal(ano, mes, quantidade=6):
    """Receita × despesa dos últimos meses (caução fica de fora — não é receita).

    Mesmas fontes de receitas_do_mes/despesas_do_mes, mas agregadas uma vez
    para a janela inteira em vez de ~8 consultas por mês. Receita fiscal:
    locação + diversos = todas as aplicações e descontos de caução do mês,
    então a classificação por origem nem precisa ser refeita aqui.
    """
    meses = _meses_da_serie(ano, mes, quantidade)
    inicio = date(meses[0][0], meses[0][1], 1)
    fim = date(meses[-1][0], meses[-1][1], monthrange(*meses[-1])[1])

    aplicacoes = _somas_por_mes(
        AplicacaoRecebimento.objects.filter(recebimento__data__range=(inicio, fim)),
        "recebimento__data",
        "valor",
    )
    descontos = _somas_por_mes(
        MovimentacaoCaucao.objects.filter(
            tipo=MovimentacaoCaucao.Tipo.DESCONTO,
            cobranca__isnull=False,
            data__range=(inicio, fim),
        ),
        "data",
        "valor",
    )
    auxilios = _somas_por_mes(
        AuxilioMotorista.objects.filter(status="recebido", data_recebimento__range=(inicio, fim)),
        "data_recebimento",
        "valor",
    )
    vendas = _somas_por_mes(
        Veiculo.objects.filter(data_venda__range=(inicio, fim)), "data_venda", "valor_venda"
    )
    manutencoes = _somas_por_mes(
        Manutencao.objects.filter(data__range=(inicio, fim), custo_real__isnull=False),
        "data",
        "custo_real",
    )
    franquias = _somas_por_mes(
        Sinistro.objects.filter(data_evento__range=(inicio, fim), franquia_valor__isnull=False),
        "data_evento",
        "franquia_valor",
    )
    custos_venda = _somas_por_mes(
        Veiculo.objects.filter(data_venda__range=(inicio, fim), custos_venda__isnull=False),
        "data_venda",
        "custos_venda",
    )
    multas_empresa = _somas_por_mes(
        Multa.objects.filter(
            responsavel=Multa.Responsavel.EMPRESA,
            data_infracao__range=(inicio, fim),
            valor__isnull=False,
        ),
        "data_infracao",
        "valor",
    )
    custos_compra = _somas_por_mes(
        Veiculo.objects.filter(data_aquisicao__range=(inicio, fim), custos_entrada__isnull=False),
        "data_aquisicao",
        "custos_entrada",
    )
    # candidatos à proteção uma vez; o recorte por mês (comprado até o fim dele,
    # não vendido antes dele) é o mesmo de despesas_do_mes, aplicado em memória
    protegidos = list(
        Veiculo.objects.filter(uso=Veiculo.Uso.LOCACAO, mensalidade_protecao__isnull=False)
        .exclude(data_venda__isnull=True, status=Veiculo.Status.VENDIDO)
        .values("mensalidade_protecao", "data_aquisicao", "data_venda")
    )

    pontos = []
    for ano_i, mes_i in meses:
        inicio_mes = date(ano_i, mes_i, 1)
        fim_mes = date(ano_i, mes_i, monthrange(ano_i, mes_i)[1])
        chave = (ano_i, mes_i)
        protecao = sum(
            (
                v["mensalidade_protecao"]
                for v in protegidos
                if (v["data_aquisicao"] is None or v["data_aquisicao"] <= fim_mes)
                and not (v["data_venda"] and v["data_venda"] < inicio_mes)
            ),
            ZERO,
        )
        receita = (
            aplicacoes.get(chave, ZERO)
            + descontos.get(chave, ZERO)
            + auxilios.get(chave, ZERO)
            + vendas.get(chave, ZERO)
        )
        despesa = (
            manutencoes.get(chave, ZERO)
            + franquias.get(chave, ZERO)
            + protecao
            + custos_venda.get(chave, ZERO)
            + multas_empresa.get(chave, ZERO)
            + custos_compra.get(chave, ZERO)
        )
        pontos.append(
            {"rotulo": f"{mes_i:02d}/{ano_i}", "receita": float(receita), "despesa": float(despesa)}
        )
    return pontos


def recebiveis_em_aberto():
    """Saldo devedor por cliente (inclui cobrança judicial)."""
    cobrancas = Cobranca.com_quitacao_anotada(
        Cobranca.objects.filter(status__in=Cobranca.STATUS_DEVIDOS)
        .select_related("cliente")
        .order_by("cliente__nome", "vencimento")
    )
    por_cliente = {}
    for cobranca in cobrancas:
        saldo = cobranca.saldo
        if saldo <= ZERO:
            continue
        registro = por_cliente.setdefault(
            cobranca.cliente,
            {"cliente": cobranca.cliente, "cobrancas": [], "total": ZERO, "judicial": ZERO},
        )
        registro["cobrancas"].append(cobranca)
        registro["total"] += saldo
        if cobranca.status == Cobranca.Status.JUDICIAL:
            registro["judicial"] += saldo
    return list(por_cliente.values())


def resumo_da_frota(hoje=None):
    fichas, media = ranking_da_frota(hoje)
    return fichas, media


def caucoes_retidas():
    anotadas = Caucao.com_saldos_anotados(Caucao.objects.select_related("alocacao__cliente"))
    return [caucao for caucao in anotadas if caucao.saldo > ZERO]
