"""Cobrança automática de excedente de km (docs.md §4.8).

Alocação `Limitado` que estoura a franquia no fechamento do mês gera uma
cobrança automaticamente — o dono pode cancelá-la na tela de cobranças se
decidir não cobrar (mesma flexibilidade dos encargos, decisão nº 13).
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Q

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.financeiro.models import Cobranca

DIAS_PRAZO_COBRANCA = 7
#: A rotina diária só gera excedente de leituras recentes — evita uma enxurrada
#: de cobranças retroativas na primeira execução após o deploy.
DIAS_RETROATIVOS_MAXIMOS = 45


@dataclass
class VinculoLimitado:
    """Com qual carro (e desde quando) o cliente da alocação limitada rodou o km lido."""

    alocacao: Alocacao
    inicio: date  # entrega da alocação ou retirada da troca temporária
    km_entrega: int  # odômetro quando o cliente pegou ESTE carro
    fim: date | None  # término/devolução, se já aconteceu
    km_saida: int | None  # odômetro registrado na saída, se houver


def _inicio_busca(registro):
    """Começo do período coberto pela leitura (leitura anterior ou 1º do mês)."""
    if registro.dias:
        return registro.data_leitura - timedelta(days=registro.dias)
    return registro.mes_referencia


def _vinculo_limitado_no_periodo(veiculo, inicio_busca, data_leitura):
    """Alocação limitada (direta ou via troca temporária) que intersecta o período.

    Também acha alocações já encerradas dentro do período — o acerto final do
    contrato limitado é cobrado na leitura seguinte ao encerramento.
    """
    candidatos = []
    alocacao = (
        Alocacao.objects.filter(
            veiculo=veiculo,
            limite_km=Alocacao.LimiteKm.LIMITADO,
            franquia_km_mensal__isnull=False,
            data_inicio__lte=data_leitura,
        )
        .filter(Q(data_termino__isnull=True) | Q(data_termino__gte=inicio_busca))
        .select_related("cliente")
        .order_by("-data_inicio")
        .first()
    )
    if alocacao:
        candidatos.append(
            VinculoLimitado(
                alocacao=alocacao,
                inicio=alocacao.data_inicio,
                km_entrega=alocacao.km_entrega,
                fim=alocacao.data_termino,
                km_saida=alocacao.km_devolucao,
            )
        )
    troca = (
        TrocaTemporaria.objects.filter(
            veiculo_substituto=veiculo,
            alocacao__limite_km=Alocacao.LimiteKm.LIMITADO,
            alocacao__franquia_km_mensal__isnull=False,
            data_retirada__lte=data_leitura,
        )
        .filter(Q(data_devolucao__isnull=True) | Q(data_devolucao__gte=inicio_busca))
        .select_related("alocacao__cliente")
        .order_by("-data_retirada")
        .first()
    )
    if troca:
        candidatos.append(
            VinculoLimitado(
                alocacao=troca.alocacao,
                inicio=troca.data_retirada,
                km_entrega=troca.km_retirada,
                fim=troca.data_devolucao,
                km_saida=troca.km_devolucao,
            )
        )
    return max(candidatos, key=lambda v: v.inicio) if candidatos else None


def calcular_excedente(registro, vinculo):
    """(km excedente, franquia do período) — só o uso do próprio cliente conta.

    Base de km: a maior entre a leitura anterior e o km de entrega do vínculo,
    para não cobrar km rodado antes da locação (motorista anterior, pátio,
    primeira leitura de um carro comprado usado). Teto: o odômetro da saída,
    quando o vínculo já terminou. Franquia rateada pelos dias em que o cliente
    de fato ficou com o carro dentro do período da leitura.
    """
    franquia_mensal = vinculo.alocacao.franquia_km_mensal
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
    franquia = round(franquia_mensal * dias / 30)
    return max(topo - base - franquia, 0), franquia


@transaction.atomic
def gerar_cobranca_excedente(registro, hoje=None):
    """Cria a cobrança do excedente do registro, se houver. Idempotente.

    Retorna a cobrança criada ou None (sem vínculo limitado, sem excedente,
    sem taxa configurada ou já gerada). O vencimento nunca nasce no passado:
    leitura lançada com atraso vence 7 dias depois de hoje, não da leitura —
    senão a rotina diária marcaria atraso/inadimplência no mesmo instante.
    """
    if registro.cobranca_excedente_id:
        return None
    hoje = hoje or date.today()
    vinculo = _vinculo_limitado_no_periodo(
        registro.veiculo, _inicio_busca(registro), registro.data_leitura
    )
    if not vinculo or not vinculo.alocacao.taxa_km_excedido:
        return None
    excedente, franquia = calcular_excedente(registro, vinculo)
    if excedente <= 0:
        return None
    alocacao = vinculo.alocacao
    valor = (alocacao.taxa_km_excedido * excedente).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    cobranca = Cobranca.objects.create(
        cliente=alocacao.cliente,
        alocacao=alocacao,
        origem=Cobranca.Origem.EXCEDENTE_KM,
        descricao=(
            f"Excedente de km {registro.veiculo.placa} {registro.mes_referencia:%m/%Y} — "
            f"{excedente} km além da franquia de {franquia} km "
            f"(R$ {alocacao.taxa_km_excedido}/km)"
        ),
        valor=valor,
        vencimento=max(registro.data_leitura, hoje) + timedelta(days=DIAS_PRAZO_COBRANCA),
    )
    registro.cobranca_excedente = cobranca
    registro.save(update_fields=["cobranca_excedente"])
    return cobranca


def gerar_excedentes_pendentes(hoje=None):
    """Varre leituras recentes ainda sem cobrança de excedente (rotina diária)."""
    from .models import RegistroKm

    hoje = hoje or date.today()
    criadas = []
    pendentes = (
        RegistroKm.objects.filter(
            cobranca_excedente__isnull=True,
            data_leitura__gte=hoje - timedelta(days=DIAS_RETROATIVOS_MAXIMOS),
        )
        .select_related("veiculo")
        .order_by("data_leitura")
    )
    for registro in pendentes:
        cobranca = gerar_cobranca_excedente(registro, hoje=hoje)
        if cobranca:
            criadas.append(cobranca)
    return criadas
