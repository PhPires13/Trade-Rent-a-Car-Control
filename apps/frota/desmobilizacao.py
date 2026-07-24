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
from django.db.models import Sum

from apps.financeiro.models import ZERO, AplicacaoRecebimento

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


def montar_ficha(veiculo, hoje=None):
    hoje = hoje or date.today()
    inicio_janela = hoje - timedelta(days=JANELA_INDICADORES_DIAS)
    ficha = FichaFinanceira(veiculo=veiculo)

    ficha.investido = (veiculo.valor_compra or ZERO) + (veiculo.custos_entrada or ZERO)

    ficha.receita_aluguel = (
        AplicacaoRecebimento.objects.filter(cobranca__alocacao__veiculo=veiculo).aggregate(
            t=Sum("valor")
        )["t"]
        or ZERO
    )
    ficha.receita_repasses = (
        AplicacaoRecebimento.objects.filter(
            cobranca__manutencao_repassada__veiculo=veiculo
        ).aggregate(t=Sum("valor"))["t"]
        or ZERO
    )
    ficha.receita_auxilios = (
        veiculo.sinistros.filter(auxilios__status="recebido").aggregate(t=Sum("auxilios__valor"))[
            "t"
        ]
        or ZERO
    )

    manutencoes = veiculo.manutencoes.all()
    ficha.despesa_manutencao = manutencoes.aggregate(t=Sum("custo_real"))["t"] or ZERO
    ficha.despesa_franquias = veiculo.sinistros.aggregate(t=Sum("franquia_valor"))["t"] or ZERO
    ficha.despesa_multas_empresa = (
        veiculo.multas.filter(responsavel="empresa").aggregate(t=Sum("valor"))["t"] or ZERO
    )
    if veiculo.mensalidade_protecao and veiculo.data_aquisicao:
        meses = max(
            1,
            (hoje.year - veiculo.data_aquisicao.year) * 12
            + hoje.month
            - veiculo.data_aquisicao.month,
        )
        ficha.despesa_protecao_estimada = veiculo.mensalidade_protecao * meses

    recentes = manutencoes.filter(data__gte=inicio_janela)
    ficha.custo_manutencao_6m = recentes.aggregate(t=Sum("custo_real"))["t"] or ZERO
    ficha.dias_parado_6m = sum(m.dias_parado for m in recentes if m.data_entrada)
    ficha.esporadicas_6m = recentes.filter(tipo="esporadica").count()
    ficha.km_rodado_6m = sum(
        r.km_utilizado or 0 for r in veiculo.registros_km.filter(mes_referencia__gte=inicio_janela)
    )
    return ficha


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


def ranking_da_frota(hoje=None):
    """Fichas avaliadas da frota de locação, piores primeiro (docs.md §4.9)."""
    veiculos = Veiculo.objects.filter(uso=Veiculo.Uso.LOCACAO).exclude(
        status=Veiculo.Status.VENDIDO
    )
    fichas = [montar_ficha(v, hoje) for v in veiculos]
    custos = [f.custo_por_km_6m for f in fichas if f.custo_por_km_6m is not None]
    media = sum(custos, ZERO) / len(custos) if custos else None
    for ficha in fichas:
        avaliar(ficha, media)
    ordem = {"vender": 0, "preparar": 1, "observar": 2, "manter": 3}
    fichas.sort(key=lambda f: (ordem[f.nivel], -(f.percentual_recuperado or Decimal("-9"))))
    return fichas, media


@transaction.atomic
def registrar_venda(veiculo, data, valor, comprador="", custos=None, km=None):
    """Vende o veículo — só sem alocação ativa; histórico preservado (docs.md §4.9)."""
    if veiculo.alocacoes.filter(status="ativa").exists():
        raise ValidationError("Encerre a alocação ativa antes de vender o veículo.")
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
