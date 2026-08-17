"""Ficha financeira e apoio à decisão de desmobilização (docs.md §4.9, decisão nº 19).

O negócio: comprar usado → alugar → vender antes da manutenção pesada.
Referência dos donos: vender quando o carro recuperou ~70–80% do investimento.
Em vez de nota opaca, indicadores objetivos + recomendação com os motivos.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum

from apps.alocacoes.models import TrocaTemporaria
from apps.financeiro.models import ZERO, AplicacaoRecebimento, MovimentacaoCaucao

from .models import Veiculo

# Limites configuráveis da recomendação (pontos em aberto nº 4 do docs.md)
FAIXA_ALVO_RECUPERACAO = Decimal("0.75")  # ~70–80% confirmado pelos donos
JANELA_INDICADORES_DIAS = 180
LIMIAR_DIAS_PARADO = 20
LIMIAR_ESPORADICAS = 2
FATOR_CUSTO_ACIMA_DA_MEDIA = Decimal("1.5")

NIVEIS = {0: "manter", 1: "observar", 2: "preparar", 3: "vender"}


@dataclass
class FichaFinanceira:
    veiculo: Veiculo
    investido: Decimal = ZERO
    receita_aluguel: Decimal = ZERO
    receita_repasses: Decimal = ZERO
    receita_auxilios: Decimal = ZERO
    despesa_manutencao: Decimal = ZERO
    despesa_franquias: Decimal = ZERO
    despesa_multas_empresa: Decimal = ZERO
    despesa_protecao_estimada: Decimal = ZERO
    custo_manutencao_6m: Decimal = ZERO
    km_rodado_6m: int = 0
    dias_parado_6m: int = 0
    esporadicas_6m: int = 0
    motivos: list = field(default_factory=list)
    nivel: str = "manter"

    do_not_call_in_templates = True

    @property
    def receita_total(self):
        return self.receita_aluguel + self.receita_repasses + self.receita_auxilios

    @property
    def despesa_total(self):
        return (
            self.despesa_manutencao
            + self.despesa_franquias
            + self.despesa_multas_empresa
            + self.despesa_protecao_estimada
        )

    @property
    def resultado_operacional(self):
        return self.receita_total - self.despesa_total

    @property
    def percentual_recuperado(self):
        if not self.investido:
            return None
        return self.resultado_operacional / self.investido

    @property
    def custo_por_km_6m(self):
        if not self.km_rodado_6m:
            return None
        return self.custo_manutencao_6m / self.km_rodado_6m

    @property
    def resultado_final(self):
        """Após a venda (docs.md §4.9)."""
        if self.veiculo.valor_venda is None:
            return None
        return (
            self.resultado_operacional
            + self.veiculo.valor_venda
            - (self.veiculo.custos_venda or ZERO)
            - self.investido
        )


def _somas(queryset, chave, campo):
    """{chave: soma do campo} numa query só — um Sum por query, sem fan-out de join."""
    return {
        linha[chave]: linha["total"]
        for linha in queryset.values(chave).annotate(total=Sum(campo))
        if linha["total"] is not None
    }


def _somas_da_frota(ids, campo, **filtros):
    """Soma por veículo partindo do próprio Veiculo (relações reversas da ficha)."""
    return _somas(Veiculo.objects.filter(pk__in=ids, **filtros), "pk", campo)


def _janela_de_manutencoes(ids, inicio_janela):
    """(custo, dias parado, esporádicas) dos últimos 6 meses por veículo, numa query."""
    hoje = date.today()  # dias_parado de manutenção em aberto conta até hoje
    janela = {}
    linhas = Veiculo.objects.filter(pk__in=ids, manutencoes__data__gte=inicio_janela).values(
        "pk",
        "manutencoes__custo_real",
        "manutencoes__tipo",
        "manutencoes__data_entrada",
        "manutencoes__data_saida",
    )
    for linha in linhas:
        custo, dias, esporadicas = janela.get(linha["pk"], (ZERO, 0, 0))
        custo += linha["manutencoes__custo_real"] or ZERO
        entrada = linha["manutencoes__data_entrada"]
        if entrada:
            dias += ((linha["manutencoes__data_saida"] or hoje) - entrada).days
        esporadicas += 1 if linha["manutencoes__tipo"] == "esporadica" else 0
        janela[linha["pk"]] = (custo, dias, esporadicas)
    return janela


def _km_rodado_na_janela(ids, inicio_janela):
    """{veiculo_id: km rodado nos últimos 6 meses} numa query (km − KM ANT das leituras)."""
    rodado = {}
    linhas = Veiculo.objects.filter(
        pk__in=ids, registros_km__mes_referencia__gte=inicio_janela
    ).values("pk", "registros_km__km", "registros_km__km_anterior")
    for linha in linhas:
        anterior = linha["registros_km__km_anterior"]
        if anterior is None:  # primeira leitura sem KM de compra: não dá para medir
            continue
        rodado[linha["pk"]] = rodado.get(linha["pk"], 0) + linha["registros_km__km"] - anterior
    return rodado


def montar_fichas_em_lote(veiculos, hoje=None):
    """Fichas de vários veículos em 9 queries fixas — na ordem em que vieram.

    Mesmos números de montar_ficha: o painel e o ranking liam a frota inteira a
    ~10 queries por carro, o que crescia linear com a frota (revisão de performance).
    """
    hoje = hoje or date.today()
    inicio_janela = hoje - timedelta(days=JANELA_INDICADORES_DIAS)
    veiculos = list(veiculos)
    fichas = [FichaFinanceira(veiculo=veiculo) for veiculo in veiculos]
    if not veiculos:
        return fichas
    ids = [veiculo.pk for veiculo in veiculos]

    aluguel = _somas(
        AplicacaoRecebimento.objects.filter(cobranca__alocacao__veiculo_id__in=ids),
        "cobranca__alocacao__veiculo",
        "valor",
    )
    # Cobrança quitada por desconto de caução também é receita do carro (docs.md §4.3)
    descontos_caucao = _somas(
        MovimentacaoCaucao.objects.filter(
            tipo=MovimentacaoCaucao.Tipo.DESCONTO, cobranca__alocacao__veiculo_id__in=ids
        ),
        "cobranca__alocacao__veiculo",
        "valor",
    )
    repasses = _somas(
        AplicacaoRecebimento.objects.filter(cobranca__manutencao_repassada__veiculo_id__in=ids),
        "cobranca__manutencao_repassada__veiculo",
        "valor",
    )
    auxilios = _somas_da_frota(
        ids, "sinistros__auxilios__valor", sinistros__auxilios__status="recebido"
    )
    manutencoes = _somas_da_frota(ids, "manutencoes__custo_real")
    franquias = _somas_da_frota(ids, "sinistros__franquia_valor")
    multas_empresa = _somas_da_frota(ids, "multas__valor", multas__responsavel="empresa")
    janela = _janela_de_manutencoes(ids, inicio_janela)
    km_rodado = _km_rodado_na_janela(ids, inicio_janela)

    for ficha in fichas:
        veiculo = ficha.veiculo
        ficha.investido = (veiculo.valor_compra or ZERO) + (veiculo.custos_entrada or ZERO)
        ficha.receita_aluguel = aluguel.get(veiculo.pk, ZERO) + descontos_caucao.get(
            veiculo.pk, ZERO
        )
        ficha.receita_repasses = repasses.get(veiculo.pk, ZERO)
        ficha.receita_auxilios = auxilios.get(veiculo.pk, ZERO)
        ficha.despesa_manutencao = manutencoes.get(veiculo.pk, ZERO)
        ficha.despesa_franquias = franquias.get(veiculo.pk, ZERO)
        ficha.despesa_multas_empresa = multas_empresa.get(veiculo.pk, ZERO)
        if veiculo.mensalidade_protecao and veiculo.data_aquisicao:
            meses = max(
                1,
                (hoje.year - veiculo.data_aquisicao.year) * 12
                + hoje.month
                - veiculo.data_aquisicao.month,
            )
            ficha.despesa_protecao_estimada = veiculo.mensalidade_protecao * meses
        custo, dias, esporadicas = janela.get(veiculo.pk, (ZERO, 0, 0))
        ficha.custo_manutencao_6m = custo
        ficha.dias_parado_6m = dias
        ficha.esporadicas_6m = esporadicas
        ficha.km_rodado_6m = km_rodado.get(veiculo.pk, 0)
    return fichas


def montar_ficha(veiculo, hoje=None):
    """Ficha de um veículo — mesmo cálculo do lote, com um carro só."""
    return montar_fichas_em_lote([veiculo], hoje)[0]


def avaliar(ficha, media_custo_km_frota=None):
    """Aplica os critérios e registra os motivos — recomendação sempre explicável."""
    pontos = 0
    perc = ficha.percentual_recuperado
    if perc is not None and perc >= FAIXA_ALVO_RECUPERACAO:
        ficha.motivos.append(
            f"Recuperou {perc:.0%} do investimento (janela de venda: ≥{FAIXA_ALVO_RECUPERACAO:.0%})"
        )
        pontos += 2
    custo_km = ficha.custo_por_km_6m
    if (
        custo_km is not None
        and media_custo_km_frota
        and custo_km > media_custo_km_frota * FATOR_CUSTO_ACIMA_DA_MEDIA
    ):
        ficha.motivos.append(
            f"Custo de manutenção/km (R$ {custo_km:.2f}) bem acima da média da frota "
            f"(R$ {media_custo_km_frota:.2f})"
        )
        pontos += 1
    if ficha.dias_parado_6m > LIMIAR_DIAS_PARADO:
        ficha.motivos.append(f"{ficha.dias_parado_6m} dias parado em oficina nos últimos 6 meses")
        pontos += 1
    if ficha.esporadicas_6m >= LIMIAR_ESPORADICAS:
        ficha.motivos.append(
            f"{ficha.esporadicas_6m} manutenções esporádicas pesadas nos últimos 6 meses"
        )
        pontos += 1
    ficha.nivel = NIVEIS[min(pontos, 3)]
    return ficha


def frota_de_locacao():
    """Frota que entra na desmobilização — locação, fora os já vendidos."""
    return Veiculo.objects.filter(uso=Veiculo.Uso.LOCACAO).exclude(status=Veiculo.Status.VENDIDO)


def media_custo_km_frota(hoje=None):
    """Média de custo de manutenção/km da frota em 2 queries, sem montar as fichas.

    Mesmo número que ranking_da_frota devolve — a ficha de um veículo só precisa
    dele para comparar, e montar a frota inteira custava ~200 queries.
    """
    hoje = hoje or date.today()
    inicio_janela = hoje - timedelta(days=JANELA_INDICADORES_DIAS)
    ids = frota_de_locacao().values_list("pk", flat=True)
    km_rodado = _km_rodado_na_janela(ids, inicio_janela)
    custos_manutencao = _somas_da_frota(
        list(km_rodado), "manutencoes__custo_real", manutencoes__data__gte=inicio_janela
    )
    custos_km = [
        custos_manutencao.get(veiculo_id, ZERO) / km for veiculo_id, km in km_rodado.items() if km
    ]
    return sum(custos_km, ZERO) / len(custos_km) if custos_km else None


def ranking_da_frota(hoje=None):
    """Fichas avaliadas da frota de locação, piores primeiro (docs.md §4.9)."""
    fichas = montar_fichas_em_lote(frota_de_locacao(), hoje)
    custos = [f.custo_por_km_6m for f in fichas if f.custo_por_km_6m is not None]
    media = sum(custos, ZERO) / len(custos) if custos else None
    for ficha in fichas:
        avaliar(ficha, media)
    ordem = {"vender": 0, "preparar": 1, "observar": 2, "manter": 3}
    fichas.sort(key=lambda f: (ordem[f.nivel], -(f.percentual_recuperado or Decimal("-9"))))
    return fichas, media


@transaction.atomic
def registrar_venda(veiculo, data, valor, comprador="", custos=None, km=None):
    """Vende o veículo — só sem alocação ativa nem troca aberta (docs.md §4.9)."""
    if veiculo.alocacoes.filter(status="ativa").exists():
        raise ValidationError("Encerre a alocação ativa antes de vender o veículo.")
    if TrocaTemporaria.objects.filter(
        Q(veiculo_substituto=veiculo) | Q(alocacao__veiculo=veiculo),
        data_devolucao__isnull=True,
    ).exists():
        raise ValidationError(
            "Veículo envolvido em troca temporária em aberto — "
            "devolva o substituto antes de vender."
        )
    if veiculo.status == Veiculo.Status.VENDIDO:
        raise ValidationError("Veículo já vendido.")
    veiculo.data_venda = data
    veiculo.valor_venda = valor
    veiculo.comprador = comprador
    veiculo.custos_venda = custos
    veiculo.km_venda = km
    if km and km > veiculo.km_atual:
        veiculo.km_atual = km
    veiculo.status = Veiculo.Status.VENDIDO
    veiculo.save()
    return veiculo
