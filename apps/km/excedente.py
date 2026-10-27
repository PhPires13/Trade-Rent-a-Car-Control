"""Cobrança automática de excedente de km (docs.md §4.8).

Alocação `Limitado` que estoura a franquia no fechamento do mês gera uma
cobrança automaticamente — o dono pode cancelá-la na tela de cobranças se
decidir não cobrar (mesma flexibilidade dos encargos, decisão nº 13).
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.financeiro.models import Cobranca
from apps.formatos import brl

from .models import AcertoExcedenteKm, RegistroKm, mes_anterior

DIAS_PRAZO_COBRANCA = 7
#: A rotina diária só gera excedente de leituras recentes — evita uma enxurrada
#: de cobranças retroativas na primeira execução após o deploy.
DIAS_RETROATIVOS_MAXIMOS = 45


#: Entre leituras de meses seguidos com esse intervalo de dias o ciclo mensal
#: está completo e vale a franquia cheia (decisão D22) — fevereiro não cobra km
#: dentro da franquia e mês de 31 dias não ganha km a mais.
DIAS_CICLO_MENSAL = range(25, 36)


@dataclass
class TermosKm:
    """Limite, franquia e taxa do contrato valendo na hora da leitura (D30)."""

    limite_km: str
    franquia_km_mensal: int | None
    taxa_km_excedido: Decimal | None


@dataclass
class VinculoLimitado:
    """Com qual carro (e desde quando) o cliente da alocação limitada rodou o km lido."""

    alocacao: Alocacao
    inicio: date  # entrega da alocação ou retirada da troca temporária
    km_entrega: int  # odômetro quando o cliente pegou ESTE carro
    fim: date | None  # término/devolução, se já aconteceu
    km_saida: int | None  # odômetro registrado na saída, se houver
    termos: TermosKm | None = None  # None = termos atuais da alocação

    @property
    def franquia_km_mensal(self):
        return (self.termos or self.alocacao).franquia_km_mensal

    @property
    def taxa_km_excedido(self):
        return (self.termos or self.alocacao).taxa_km_excedido


def _inicio_busca(registro):
    """Começo do período coberto pela leitura (leitura anterior ou 1º do mês)."""
    if registro.dias:
        return registro.data_leitura - timedelta(days=registro.dias)
    return registro.mes_referencia


def _momentos_dos_termos(registro):
    """Até quando uma mudança no contrato ainda vale para esta leitura (D30).

    O fim do dia da leitura ou o instante em que ela foi lançada, o que vier
    antes: mudar o contrato depois não reavalia leitura já feita. O segundo
    momento (o lançamento) serve quando o contrato só foi cadastrado depois
    do dia da leitura (lançamento retroativo).
    """
    fim_do_dia = timezone.make_aware(datetime.combine(registro.data_leitura, time.max))
    criada = None
    if registro.pk:
        criada = (
            registro.history.order_by("history_date").values_list("history_date", flat=True).first()
        )
    if criada is None:
        return (fim_do_dia,)
    return (min(fim_do_dia, criada), criada)


def _termos_em(alocacao, momentos):
    """Termos de km da alocação no primeiro momento em que ela já existia no
    sistema (contrato cadastrado depois da leitura: os termos do cadastro)."""
    for momento in momentos:
        if momento is None:
            continue
        try:
            versao = alocacao.history.as_of(momento)
        except Alocacao.DoesNotExist:
            continue
        return TermosKm(versao.limite_km, versao.franquia_km_mensal, versao.taxa_km_excedido)
    versao = alocacao.history.order_by("history_date").first() or alocacao
    return TermosKm(versao.limite_km, versao.franquia_km_mensal, versao.taxa_km_excedido)


def _vinculos_limitados_no_periodo(veiculo, inicio_busca, data_leitura, momentos=()):
    """Alocações limitadas (diretas ou via troca temporária) que intersectam o
    período, da mais antiga para a mais recente — cada cliente com sua base e
    seu topo (C15: A devolveu, B pegou o mesmo carro antes da leitura).

    Também acha alocações já encerradas dentro do período — o acerto final do
    contrato limitado é cobrado na leitura seguinte ao encerramento. Os termos
    (limitado, franquia, taxa) são os que valiam na leitura, não os de hoje.
    """
    vinculos = []
    alocacoes = (
        Alocacao.objects.filter(veiculo=veiculo, data_inicio__lte=data_leitura)
        .filter(Q(data_termino__isnull=True) | Q(data_termino__gte=inicio_busca))
        .select_related("cliente")
    )
    for alocacao in alocacoes:
        vinculos.append(
            VinculoLimitado(
                alocacao=alocacao,
                inicio=alocacao.data_inicio,
                km_entrega=alocacao.km_entrega,
                fim=alocacao.data_termino,
                km_saida=alocacao.km_devolucao,
            )
        )
    trocas = (
        TrocaTemporaria.objects.filter(veiculo_substituto=veiculo, data_retirada__lte=data_leitura)
        .filter(Q(data_devolucao__isnull=True) | Q(data_devolucao__gte=inicio_busca))
        .select_related("alocacao__cliente")
    )
    for troca in trocas:
        vinculos.append(
            VinculoLimitado(
                alocacao=troca.alocacao,
                inicio=troca.data_retirada,
                km_entrega=troca.km_retirada,
                fim=troca.data_devolucao,
                km_saida=troca.km_devolucao,
            )
        )
    limitados = []
    for vinculo in vinculos:
        vinculo.termos = _termos_em(vinculo.alocacao, momentos)
        if (
            vinculo.termos.limite_km == Alocacao.LimiteKm.LIMITADO
            and vinculo.termos.franquia_km_mensal
        ):
            limitados.append(vinculo)
    return sorted(limitados, key=lambda v: v.inicio)


def _ciclo_mensal_completo(registro):
    """Leitura que fecha um mês inteiro: a anterior é do mês imediatamente antes
    e o intervalo fica em torno de um mês (D22)."""
    if registro.dias not in DIAS_CICLO_MENSAL:
        return False
    anterior = registro.registro_anterior()
    return anterior is not None and anterior.mes_referencia == mes_anterior(registro.mes_referencia)


def calcular_excedente(registro, vinculo):
    """(km excedente, franquia do período) — só o uso do próprio cliente conta.

    Base de km: a maior entre a leitura anterior e o km de entrega do vínculo,
    para não cobrar km rodado antes da locação (motorista anterior, pátio,
    primeira leitura de um carro comprado usado). Teto: o odômetro da saída,
    quando o vínculo já terminou. Mês completo com o cliente usa a franquia
    cheia (D22); entrega, devolução, troca ou mês faltando rateiam a franquia
    pelos dias em que o cliente de fato ficou com o carro (dias/30).
    """
    franquia_mensal = vinculo.franquia_km_mensal
    fim_vigencia = vinculo.fim or registro.data_leitura
    if fim_vigencia < registro.data_leitura and vinculo.km_saida is None:
        return 0, franquia_mensal  # saiu sem odômetro anotado: não dá para apurar
    base = max(registro.km_anterior or 0, vinculo.km_entrega or 0)
    topo = registro.km if vinculo.km_saida is None else min(registro.km, vinculo.km_saida)
    data_base = (
        registro.data_leitura - timedelta(days=registro.dias) if registro.dias else vinculo.inicio
    )
    dias = (min(registro.data_leitura, fim_vigencia) - max(data_base, vinculo.inicio)).days
    if dias <= 0:
        return 0, franquia_mensal
    periodo_inteiro = vinculo.inicio <= data_base and fim_vigencia >= registro.data_leitura
    if periodo_inteiro and _ciclo_mensal_completo(registro):
        franquia = franquia_mensal
    else:
        franquia = round(franquia_mensal * dias / 30)
    return max(topo - base - franquia, 0), franquia


def _criar_cobranca(registro, vinculo, excedente, franquia, hoje):
    alocacao = vinculo.alocacao
    taxa = vinculo.taxa_km_excedido
    valor = (taxa * excedente).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return Cobranca.objects.create(
        cliente=alocacao.cliente,
        alocacao=alocacao,
        origem=Cobranca.Origem.EXCEDENTE_KM,
        descricao=(
            f"Excedente de km {registro.veiculo.placa} {registro.mes_referencia:%m/%Y} — "
            f"{excedente} km além da franquia de {franquia} km "
            f"({brl(taxa)}/km)"
        ),
        valor=valor,
        vencimento=max(registro.data_leitura, hoje) + timedelta(days=DIAS_PRAZO_COBRANCA),
    )


@transaction.atomic
def gerar_cobrancas_excedente(registro, hoje=None):
    """Cria as cobranças de excedente da leitura que ainda faltam. Idempotente.

    Uma por contrato limitado que usou o carro no período: a de quem estava
    com ele por último fica em `registro.cobranca_excedente`; a de quem
    devolveu antes vira um AcertoExcedenteKm (C15). Os termos usados são os
    do contrato na hora da leitura (D30). O vencimento nunca nasce no
    passado: leitura lançada com atraso vence 7 dias depois de hoje, não da
    leitura — senão a rotina diária marcaria atraso/inadimplência na hora.
    Devolve as cobranças criadas, a principal primeiro.
    """
    hoje = hoje or timezone.localdate()
    momentos = _momentos_dos_termos(registro)
    vinculos = _vinculos_limitados_no_periodo(
        registro.veiculo, _inicio_busca(registro), registro.data_leitura, momentos
    )
    if not vinculos:
        return []
    principal, anteriores = vinculos[-1], vinculos[:-1]
    # Contrato que já tem cobrança desta leitura (principal ou acerto, mesmo
    # cancelada pelo dono) não é cobrado de novo — inclusive quando outro
    # contrato do período só foi cadastrado depois e virou "o último".
    cobertas = set(registro.acertos_excedente.values_list("alocacao_id", flat=True))
    if registro.cobranca_excedente_id:
        cobertas.update(
            Cobranca.objects.filter(pk=registro.cobranca_excedente_id).values_list(
                "alocacao_id", flat=True
            )
        )
    criadas = []
    for vinculo in [principal, *anteriores]:
        alocacao = vinculo.alocacao
        if alocacao.pk in cobertas or not vinculo.taxa_km_excedido:
            continue
        excedente, franquia = calcular_excedente(registro, vinculo)
        if excedente <= 0:
            continue
        cobranca = _criar_cobranca(registro, vinculo, excedente, franquia, hoje)
        if vinculo is principal and not registro.cobranca_excedente_id:
            registro.cobranca_excedente = cobranca
            registro.save(update_fields=["cobranca_excedente"])
        else:
            AcertoExcedenteKm.objects.create(
                registro=registro, alocacao=alocacao, cobranca=cobranca
            )
        cobertas.add(alocacao.pk)
        criadas.append(cobranca)
    return criadas


def gerar_cobranca_excedente(registro, hoje=None):
    """Como gerar_cobrancas_excedente, devolvendo só a cobrança principal criada
    (ou None: sem vínculo limitado, sem excedente, sem taxa ou já gerada)."""
    principal_antes = registro.cobranca_excedente_id
    gerar_cobrancas_excedente(registro, hoje=hoje)
    if principal_antes is None and registro.cobranca_excedente_id:
        return registro.cobranca_excedente
    return None


def gerar_excedentes_pendentes(hoje=None):
    """Varre leituras recentes e cria o excedente que ainda faltar (rotina diária).

    Olha também leituras que já têm a cobrança principal: o acerto de quem
    devolveu o carro no meio do período pode estar faltando (C15). Os termos
    são os da hora da leitura, então reavaliar não cobra mudança de contrato
    feita depois (D30).
    """
    hoje = hoje or timezone.localdate()
    criadas = []
    recentes = (
        RegistroKm.objects.filter(
            data_leitura__gte=hoje - timedelta(days=DIAS_RETROATIVOS_MAXIMOS),
        )
        .select_related("veiculo")
        .order_by("data_leitura")
    )
    for registro in recentes:
        criadas.extend(gerar_cobrancas_excedente(registro, hoje=hoje))
    return criadas
