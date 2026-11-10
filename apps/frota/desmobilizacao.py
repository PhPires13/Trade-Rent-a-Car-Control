"""Ficha financeira e apoio à decisão de desmobilização (docs.md §4.9, decisão nº 19).

O negócio: comprar usado → alugar → vender antes da manutenção pesada.
Referência dos donos: vender quando o carro recuperou ~70–80% do investimento.
Em vez de nota opaca, indicadores objetivos + recomendação com os motivos.
"""

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import BooleanField, ExpressionWrapper, Max, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.text import capfirst

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.financeiro.models import ZERO, AplicacaoRecebimento, Cobranca, MovimentacaoCaucao
from apps.formatos import brl, milhar
from apps.manutencao.models import Manutencao
from apps.multas.models import Multa
from apps.sinistros.models import AuxilioMotorista

from .models import PagamentoIpva, Veiculo

# Limites configuráveis da recomendação (docs.md §8, ponto em aberto nº 4)
FAIXA_ALVO_RECUPERACAO = Decimal("0.75")  # ~70–80% confirmado pelos donos
JANELA_INDICADORES_DIAS = 180
LIMIAR_DIAS_PARADO = 20
LIMIAR_ESPORADICAS = 2
FATOR_CUSTO_ACIMA_DA_MEDIA = Decimal("1.5")

#: pontos de alerta → nível da recomendação (3 ou mais = vender)
NIVEIS = {0: "manter", 1: "observar", 2: "preparar", 3: "vender"}

#: conserto e franquia cobrados do cliente: na ficha vão para "Consertos e
#: franquias", mesmo quando a cobrança aponta para a alocação
ORIGENS_DE_REPASSE = (Cobranca.Origem.REPASSE_MANUTENCAO, Cobranca.Origem.REPASSE_SINISTRO)


# Critérios de despesa — fonte única da ficha do carro e dos relatórios do mês
# (apps/relatorios/services.py), para os dois fecharem entre si.


def manutencoes_que_sao_despesa():
    """Manutenções com custo real, menos o conserto pago pela cota da proteção.

    Quando a proteção é acionada, a cota paga à associação já é despesa no
    sinistro (franquia_valor). O conserto ligado a esse sinistro com origem
    "Evento da proteção" repetiria o mesmo dinheiro — só conta o que foi pago
    por fora (origem "Particular").
    """
    return Manutencao.objects.filter(custo_real__isnull=False).exclude(
        origem_custo=Manutencao.OrigemCusto.PROTECAO, sinistro__franquia_valor__isnull=False
    )


def multas_que_sao_despesa():
    """Multas que a empresa assumiu e que continuam devidas.

    Advertência, cancelada ou não exigível: não há o que pagar, então não é
    despesa (mesma regra do atalho "Pagar" da lista de multas). Multa já cobrada
    do motorista numa ND também não: o dinheiro volta pela cobrança, e contar
    as duas coisas faria a mesma multa aparecer como despesa e como receita.
    """
    return (
        Multa.objects.filter(responsavel=Multa.Responsavel.EMPRESA, valor__isnull=False)
        .exclude(resultado__in=Multa.RESULTADOS_ANULADORES)
        .exclude(itens_nd__isnull=False)
    )


def ipvas_pagos():
    """Parcelas de IPVA pagas — despesa no mês do pagamento (decisão nº 21)."""
    return PagamentoIpva.objects.filter(pago_em__isnull=False, valor__isnull=False)


def meses_de_protecao(veiculo, hoje):
    """Mensalidades da proteção do mês da compra ao da venda (ou até hoje), os dois inclusive.

    Mesmo recorte do relatório mensal: cada mês em que o carro esteve na frota
    paga uma mensalidade; depois de vendido, não paga mais. Critério único:
    sem data de compra = sem proteção a lançar (o cadastro exige a data
    quando há mensalidade; a ficha avisa "proteção não calculada"). O mesmo vale
    para carro vendido sem data da venda (o Admin passa a exigir a data).
    """
    if not veiculo.data_aquisicao:
        return 0
    if veiculo.status == Veiculo.Status.VENDIDO and not veiculo.data_venda:
        # vendido sem data (dado antigo/Admin): o relatório mensal já não conta
        # esse carro; somar "até hoje" aqui faria a ficha não bater com ele
        return 0
    fim = min(veiculo.data_venda, hoje) if veiculo.data_venda else hoje
    meses = (
        (fim.year - veiculo.data_aquisicao.year) * 12 + fim.month - veiculo.data_aquisicao.month + 1
    )
    return max(meses, 0)


@dataclass
class FichaFinanceira:
    veiculo: Veiculo
    investido: Decimal = ZERO
    receita_aluguel: Decimal = ZERO
    #: excedente de km, encargo por atraso e outras cobranças do contrato
    receita_extras_locacao: Decimal = ZERO
    receita_repasses: Decimal = ZERO
    receita_auxilios: Decimal = ZERO
    despesa_manutencao: Decimal = ZERO
    despesa_franquias: Decimal = ZERO
    despesa_multas_empresa: Decimal = ZERO
    despesa_ipva: Decimal = ZERO
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
        return (
            self.receita_aluguel
            + self.receita_extras_locacao
            + self.receita_repasses
            + self.receita_auxilios
        )

    @property
    def despesa_total(self):
        return (
            self.despesa_manutencao
            + self.despesa_franquias
            + self.despesa_multas_empresa
            + self.despesa_ipva
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
    def prejuizo_operacional(self):
        """Quanto falta para empatar (positivo) quando o carro ainda deu mais gasto que receita."""
        resultado = self.resultado_operacional
        return -resultado if resultado < 0 else None

    @property
    def na_janela_de_venda(self):
        """Já devolveu a faixa-alvo do investimento (~75%) — o principal sinal de venda."""
        perc = self.percentual_recuperado
        return perc is not None and perc >= FAIXA_ALVO_RECUPERACAO

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
    hoje = timezone.localdate()  # dias_parado de manutenção em aberto conta até hoje
    janela = {}
    linhas = Veiculo.objects.filter(pk__in=ids, manutencoes__data__gte=inicio_janela).values(
        "pk",
        "manutencoes__custo_real",
        "manutencoes__tipo",
        "manutencoes__data_entrada",
        "manutencoes__data_saida",
        "manutencoes__origem_custo",
        "manutencoes__sinistro__franquia_valor",
    )
    for linha in linhas:
        custo, dias, esporadicas = janela.get(linha["pk"], (ZERO, 0, 0))
        # conserto pago pela cota da proteção já é despesa no sinistro (D07):
        # mesmo critério de manutencoes_que_sao_despesa; dias/esporádicas contam
        pela_cota = (
            linha["manutencoes__origem_custo"] == Manutencao.OrigemCusto.PROTECAO
            and linha["manutencoes__sinistro__franquia_valor"] is not None
        )
        if not pela_cota:
            custo += linha["manutencoes__custo_real"] or ZERO
        entrada = linha["manutencoes__data_entrada"]
        if entrada:
            dias += ((linha["manutencoes__data_saida"] or hoje) - entrada).days
        esporadicas += 1 if linha["manutencoes__tipo"] == Manutencao.Tipo.ESPORADICA else 0
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
    """Fichas de vários veículos em 10 queries fixas — na ordem em que vieram.

    Mesmos números de montar_ficha: o painel e o ranking liam a frota inteira a
    ~10 queries por carro, o que crescia linear com a frota (revisão de performance).
    """
    hoje = hoje or timezone.localdate()
    inicio_janela = hoje - timedelta(days=JANELA_INDICADORES_DIAS)
    veiculos = list(veiculos)
    fichas = [FichaFinanceira(veiculo=veiculo) for veiculo in veiculos]
    if not veiculos:
        return fichas
    ids = [veiculo.pk for veiculo in veiculos]

    # cobranças do contrato: o aluguel semanal numa linha; excedente de km, encargo
    # e outras do contrato em outra; conserto/franquia que aponte para a alocação
    # soma nos repasses — uma query só, separada pela origem
    aluguel, extras_locacao, repasses_da_locacao = {}, {}, {}
    destinos = {
        Cobranca.Origem.ALUGUEL: aluguel,
        **dict.fromkeys(ORIGENS_DE_REPASSE, repasses_da_locacao),
    }
    for linha in (
        AplicacaoRecebimento.objects.filter(cobranca__alocacao__veiculo_id__in=ids)
        .values("cobranca__alocacao__veiculo", "cobranca__origem")
        .annotate(total=Sum("valor"))
    ):
        destino = destinos.get(linha["cobranca__origem"], extras_locacao)
        chave = linha["cobranca__alocacao__veiculo"]
        destino[chave] = destino.get(chave, ZERO) + (linha["total"] or ZERO)
    # Cobrança quitada por desconto de caução também é receita do carro (docs.md §4.3):
    # a da locação soma no aluguel (ou nos extras do contrato); repasse de manutenção
    # e franquia (sem alocação) somam nos repasses — uma query só, separada pelo tipo
    descontos_aluguel, descontos_extras, descontos_repasse = {}, {}, {}
    for linha in (
        MovimentacaoCaucao.objects.filter(tipo=MovimentacaoCaucao.Tipo.DESCONTO)
        .filter(
            Q(cobranca__alocacao__veiculo_id__in=ids)
            | Q(cobranca__manutencao_repassada__veiculo_id__in=ids)
            | Q(cobranca__sinistro_franquia__veiculo_id__in=ids, cobranca__alocacao__isnull=True)
        )
        .annotate(
            veiculo_do_desconto=Coalesce(
                "cobranca__alocacao__veiculo",
                "cobranca__manutencao_repassada__veiculo",
                "cobranca__sinistro_franquia__veiculo",
            ),
            de_locacao=ExpressionWrapper(
                Q(cobranca__alocacao__isnull=False), output_field=BooleanField()
            ),
        )
        .values("veiculo_do_desconto", "de_locacao", "cobranca__origem")
        .annotate(total=Sum("valor"))
    ):
        if not linha["de_locacao"] or linha["cobranca__origem"] in ORIGENS_DE_REPASSE:
            destino = descontos_repasse
        elif linha["cobranca__origem"] == Cobranca.Origem.ALUGUEL:
            destino = descontos_aluguel
        else:
            destino = descontos_extras
        chave = linha["veiculo_do_desconto"]
        destino[chave] = destino.get(chave, ZERO) + (linha["total"] or ZERO)
    # repasses de manutenção e a franquia do sinistro cobrada do cliente (D09) —
    # sem alocação na cobrança (a com alocação já entrou acima), para anular a
    # franquia que já é despesa do carro; uma query só para as duas origens
    repasses = _somas(
        AplicacaoRecebimento.objects.filter(
            Q(cobranca__manutencao_repassada__veiculo_id__in=ids)
            | Q(cobranca__sinistro_franquia__veiculo_id__in=ids),
            cobranca__alocacao__isnull=True,
        ).annotate(
            veiculo_do_repasse=Coalesce(
                "cobranca__manutencao_repassada__veiculo", "cobranca__sinistro_franquia__veiculo"
            )
        ),
        "veiculo_do_repasse",
        "valor",
    )
    auxilios = _somas_da_frota(
        ids,
        "sinistros__auxilios__valor",
        sinistros__auxilios__status=AuxilioMotorista.Status.RECEBIDO,
    )
    manutencoes = _somas(
        manutencoes_que_sao_despesa().filter(veiculo_id__in=ids), "veiculo", "custo_real"
    )
    franquias = _somas_da_frota(ids, "sinistros__franquia_valor")
    multas_empresa = _somas(multas_que_sao_despesa().filter(veiculo_id__in=ids), "veiculo", "valor")
    ipvas = _somas(ipvas_pagos().filter(veiculo_id__in=ids), "veiculo", "valor")
    janela = _janela_de_manutencoes(ids, inicio_janela)
    km_rodado = _km_rodado_na_janela(ids, inicio_janela)

    for ficha in fichas:
        veiculo = ficha.veiculo
        ficha.investido = (veiculo.valor_compra or ZERO) + (veiculo.custos_entrada or ZERO)
        ficha.receita_aluguel = aluguel.get(veiculo.pk, ZERO) + descontos_aluguel.get(
            veiculo.pk, ZERO
        )
        ficha.receita_extras_locacao = extras_locacao.get(veiculo.pk, ZERO) + descontos_extras.get(
            veiculo.pk, ZERO
        )
        ficha.receita_repasses = (
            repasses.get(veiculo.pk, ZERO)
            + repasses_da_locacao.get(veiculo.pk, ZERO)
            + descontos_repasse.get(veiculo.pk, ZERO)
        )
        ficha.receita_auxilios = auxilios.get(veiculo.pk, ZERO)
        ficha.despesa_manutencao = manutencoes.get(veiculo.pk, ZERO)
        ficha.despesa_franquias = franquias.get(veiculo.pk, ZERO)
        ficha.despesa_multas_empresa = multas_empresa.get(veiculo.pk, ZERO)
        ficha.despesa_ipva = ipvas.get(veiculo.pk, ZERO)
        if veiculo.mensalidade_protecao:
            ficha.despesa_protecao_estimada = veiculo.mensalidade_protecao * meses_de_protecao(
                veiculo, hoje
            )
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
    if ficha.na_janela_de_venda:
        ficha.motivos.append(
            f"Recuperou {ficha.percentual_recuperado:.0%} do investimento "
            f"(janela de venda: ≥{FAIXA_ALVO_RECUPERACAO:.0%})"
        )
        pontos += 2
    custo_km = ficha.custo_por_km_6m
    if (
        custo_km is not None
        and media_custo_km_frota
        and custo_km > media_custo_km_frota * FATOR_CUSTO_ACIMA_DA_MEDIA
    ):
        ficha.motivos.append(
            f"Custo de manutenção/km ({brl(custo_km)}) bem acima da média da frota "
            f"({brl(media_custo_km_frota)})"
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
    hoje = hoje or timezone.localdate()
    inicio_janela = hoje - timedelta(days=JANELA_INDICADORES_DIAS)
    ids = frota_de_locacao().values_list("pk", flat=True)
    km_rodado = _km_rodado_na_janela(ids, inicio_janela)
    # sem o conserto pago pela cota da proteção (D07), como _janela_de_manutencoes
    custos_manutencao = _somas(
        manutencoes_que_sao_despesa().filter(
            veiculo_id__in=list(km_rodado), data__gte=inicio_janela
        ),
        "veiculo",
        "custo_real",
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
    pontos_do_nivel = {nivel: pontos for pontos, nivel in NIVEIS.items()}
    fichas.sort(
        key=lambda f: (-pontos_do_nivel[f.nivel], -(f.percentual_recuperado or Decimal("-9")))
    )
    return fichas, media


#: Campos que a venda grava — validados antes do save (casas decimais, tamanho, KM ≥ 0).
CAMPOS_DA_VENDA = ("data_venda", "valor_venda", "custos_venda", "comprador", "km_venda")


def _validar_campos_da_venda(veiculo):
    """Erro legível em vez de erro 500 do banco (KM negativo, valor ou comprador grandes demais)."""
    outros = [campo.name for campo in Veiculo._meta.fields if campo.name not in CAMPOS_DA_VENDA]
    try:
        veiculo.clean_fields(exclude=outros)
    except ValidationError as erro:
        raise ValidationError(
            [
                f"{capfirst(Veiculo._meta.get_field(campo).verbose_name)}: {' '.join(mensagens)}"
                for campo, mensagens in erro.message_dict.items()
            ]
        ) from None


def oficina_impede_venda(veiculo):
    """Manutenção ainda aberta (carro na oficina) — ela ficaria aberta para sempre."""
    from apps.manutencao.services import oficina_por_veiculo  # manutencao importa frota

    return oficina_por_veiculo([veiculo.pk]).get(veiculo.pk)


def texto_oficina_impede_venda(manutencao):
    return (
        f"O carro ainda está na oficina (desde {manutencao.data_entrada:%d/%m}). "
        "Registre “Carro voltou da oficina” antes de vender."
    )


def ultimo_lancamento_do_carro(veiculo, so_com_km=False):
    """(rótulo, data) do lançamento mais recente do carro, ou None se não houver.

    Leitura de KM, entrega/devolução, troca (como substituto), vistoria,
    manutenção e a saída da oficina — a venda não pode ser antes de nenhum deles.
    so_com_km: só os lançamentos que anotaram o KM (base do aviso de KM da venda).
    """
    from apps.km.correcao import _fontes_de_km  # km importa frota

    fontes = list(_fontes_de_km())
    if not so_com_km:
        fontes.append(("saída da oficina", Manutencao, "veiculo_id", None, "data_saida"))
    mais_recente = None
    for rotulo, modelo, campo_carro, campo_km, campo_data in fontes:
        filtro = {campo_carro: veiculo.pk}
        if so_com_km:
            filtro[f"{campo_km}__isnull"] = False
        data = modelo.objects.filter(**filtro).aggregate(d=Max(campo_data))["d"]
        if data and (mais_recente is None or data > mais_recente[1]):
            mais_recente = (rotulo, data)
    return mais_recente


def suspeita_do_km_da_venda(veiculo, km, data):
    """Aviso quando o KM da venda foge do normal (mesma régua da manutenção, D27)."""
    from apps.alocacoes.km_aviso import suspeita_de_km
    from apps.km.correcao import SALTO_MAXIMO_SEM_LEITURA

    if km is None or km <= veiculo.km_atual or not veiculo.km_atual:
        return None  # sem KM nenhum no cadastro não há com o que comparar
    # base: o último lançamento que ANOTOU o KM (uma saída da oficina sem KM não
    # diz quanto o carro marcava — a mensagem falaria de uma anotação que não houve)
    ultimo = ultimo_lancamento_do_carro(veiculo, so_com_km=True)
    data_base = ultimo[1] if ultimo else veiculo.data_aquisicao
    if data_base is not None:
        return suspeita_de_km(km, data, veiculo.km_atual, data_base, "na última anotação de KM")
    if km - veiculo.km_atual <= SALTO_MAXIMO_SEM_LEITURA:
        return None
    return (
        f"Você digitou {milhar(km)} km, mas o carro está cadastrado com "
        f"{milhar(veiculo.km_atual)} km — seriam {milhar(km - veiculo.km_atual)} km a mais. "
        "Confira se não sobrou um dígito. Se estiver certo, marque “Confirmo o KM” e "
        "salve de novo."
    )


def _validar_data_e_km_da_venda(veiculo, data, km, hoje=None, confirmar_km=False):
    """Data entre o último lançamento do carro e hoje; KM nunca abaixo do atual.

    A venda não se desfaz: um KM fora do normal (dígito a mais) só passa com
    "Confirmo o KM", senão ficaria no carro para sempre.
    """
    hoje = hoje or timezone.localdate()
    erros = []
    if data > hoje:
        erros.append("A data da venda não pode ser depois de hoje.")
    if veiculo.data_aquisicao and data < veiculo.data_aquisicao:
        erros.append(f"A venda não pode ser antes da compra ({veiculo.data_aquisicao:%d/%m/%Y}).")
    ultimo = ultimo_lancamento_do_carro(veiculo)
    if ultimo and data < ultimo[1]:
        erros.append(
            f"O carro tem {ultimo[0]} registrada em {ultimo[1]:%d/%m/%Y} — "
            "a venda não pode ser antes disso."
        )
    # KM negativo fica para _validar_campos_da_venda (mensagem do campo "KM na venda")
    if km is not None and 0 <= km < veiculo.km_atual:
        erros.append("KM da venda menor que o KM atual do carro — confira o painel.")
    if erros:
        raise ValidationError(erros)
    aviso = None if confirmar_km else suspeita_do_km_da_venda(veiculo, km, data)
    if aviso:
        raise ValidationError(aviso, code="km_suspeito")


@transaction.atomic
def registrar_venda(veiculo, data, valor, comprador="", custos=None, km=None, confirmar_km=False):
    """Vende o veículo — só sem alocação ativa, troca aberta nem oficina (docs.md §4.9)."""
    if veiculo.alocacoes.filter(status=Alocacao.Status.ATIVA).exists():
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
    na_oficina = oficina_impede_venda(veiculo)
    if na_oficina:
        raise ValidationError(texto_oficina_impede_venda(na_oficina))
    # antes de mexer no objeto: a tela reaproveita o veículo para remontar a ficha
    _validar_data_e_km_da_venda(veiculo, data, km, confirmar_km=confirmar_km)
    # a venda não se desfaz: valor zero/negativo estragaria o resultado final do carro
    erros_valor = []
    if valor is None or valor <= 0:
        erros_valor.append("Informe quanto recebeu na venda (valor maior que zero).")
    if custos is not None and custos < 0:
        erros_valor.append("Os custos da venda não podem ser negativos.")
    if erros_valor:
        raise ValidationError(erros_valor)
    veiculo.data_venda = data
    veiculo.valor_venda = valor
    veiculo.comprador = comprador
    veiculo.custos_venda = custos
    veiculo.km_venda = km
    _validar_campos_da_venda(veiculo)
    if km and km > veiculo.km_atual:
        veiculo.km_atual = km
    veiculo.status = Veiculo.Status.VENDIDO
    veiculo.save()
    return veiculo
