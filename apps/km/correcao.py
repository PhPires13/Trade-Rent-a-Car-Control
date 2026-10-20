"""KM fora do normal e correção de leitura errada (proposta de UX nº 4).

Um zero a mais no odômetro (120.000 em vez de 12.000) sobe o KM atual do
carro, deixa as preventivas "Vencida" e pode gerar cobrança de excedente.
Por isso: a leitura que foge do normal pede confirmação por extenso antes de
gravar, e a leitura gravada errada pode ser corrigida ou apagada pela tela —
refazendo a cadeia de KM ANT/dias, o KM atual do carro e o excedente em aberto.
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max

from apps.financeiro.models import ZERO, Cobranca
from apps.formatos import brl, milhar

from .excedente import gerar_cobranca_excedente
from .models import RegistroKm

#: Ninguém roda mais que isso por dia com carro de aplicativo — acima, é erro de digitação.
LIMITE_KM_POR_DIA = 1_000
#: Salto maior que esse múltiplo da média do próprio carro pede confirmação...
MULTIPLO_DA_MEDIA = 3
#: ...mas só quando o trecho é grande o bastante para a média dizer algo.
KM_MINIMO_PARA_COMPARAR_MEDIA = 1_000
#: Sem leitura anterior, compara com o KM do cadastro.
SALTO_MAXIMO_SEM_LEITURA = 15_000


def _ha_quanto_tempo(dias):
    if dias <= 0:
        return "no mesmo dia"
    if dias == 1:
        return "há 1 dia"
    return f"há {dias} dias"


def media_diaria_do_carro(registro):
    """Média de km/dia das leituras anteriores a esta (None se não houver base)."""
    anteriores = RegistroKm.objects.filter(
        veiculo=registro.veiculo,
        mes_referencia__lt=registro.mes_referencia,
        km_anterior__isnull=False,
        dias__gt=0,
    ).values_list("km", "km_anterior", "dias")
    total_km = total_dias = 0
    for km, km_anterior, dias in anteriores:
        total_km += km - km_anterior
        total_dias += dias
    if total_dias < 7:  # menos de uma semana de histórico não é média confiável
        return None
    return total_km / total_dias


def suspeita_do_km(registro):
    """Texto do aviso quando o KM digitado foge do normal; None se está ok.

    Espera o registro já validado (full_clean) — KM menor que a leitura
    anterior é bloqueado lá, não chega aqui.
    """
    digitado = f"Você digitou {milhar(registro.km)} km"
    anterior = registro.registro_anterior()
    if anterior is None:
        if registro.registro_seguinte() is not None:
            return None  # mês esquecido: o clean() já o limitou pela leitura de depois
        veiculo = registro.veiculo
        # Na correção o KM atual pode ser justamente o da leitura errada — vale o de compra.
        cadastro = veiculo.km_compra or 0
        if registro.pk is None:
            cadastro = max(veiculo.km_atual, cadastro)
        elif not veiculo.km_compra:
            return None  # corrigindo a 1ª leitura de carro sem KM de compra: não há base
        if registro.km < cadastro:
            return (
                f"{digitado}, mas o cadastro do carro está com {milhar(cadastro)} km "
                f"(o número é menor). Confirma?"
            )
        if registro.km - cadastro > SALTO_MAXIMO_SEM_LEITURA:
            return (
                f"{digitado}, mas o cadastro do carro está com {milhar(cadastro)} km — "
                f"seriam {milhar(registro.km - cadastro)} km a mais. Confirma?"
            )
        return None

    dias = (registro.data_leitura - anterior.data_leitura).days
    rodado = registro.km - anterior.km
    media = rodado / max(dias, 1)
    contexto = (
        f"{digitado} — o carro estava com {milhar(anterior.km)} km {_ha_quanto_tempo(dias)} "
        f"(em {anterior.data_leitura:%d/%m/%Y})."
    )
    if media > LIMITE_KM_POR_DIA:
        return (
            f"{contexto} Seriam {milhar(rodado)} km rodados, uns {milhar(round(media))} km "
            f"por dia. Confirma?"
        )
    media_carro = media_diaria_do_carro(registro)
    if (
        media_carro
        and rodado >= KM_MINIMO_PARA_COMPARAR_MEDIA
        and media > MULTIPLO_DA_MEDIA * media_carro
    ):
        return (
            f"{contexto} Seriam {milhar(rodado)} km rodados, uns {milhar(round(media))} km "
            f"por dia — mais de {MULTIPLO_DA_MEDIA} vezes o normal deste carro "
            f"(cerca de {milhar(round(media_carro))} km por dia). Confirma?"
        )
    return None


# --- Correção ----------------------------------------------------------------


def km_atual_recalculado(veiculo):
    """Maior odômetro conhecido do carro em todos os lançamentos do sistema."""
    from apps.alocacoes.models import Alocacao, TrocaTemporaria, Vistoria
    from apps.manutencao.models import Manutencao

    candidatos = [veiculo.km_compra or 0]
    candidatos += (
        Alocacao.objects.filter(veiculo=veiculo)
        .aggregate(a=Max("km_entrega"), b=Max("km_devolucao"))
        .values()
    )
    candidatos += (
        TrocaTemporaria.objects.filter(veiculo_substituto=veiculo)
        .aggregate(a=Max("km_retirada"), b=Max("km_devolucao"))
        .values()
    )
    for consulta in (
        RegistroKm.objects.filter(veiculo=veiculo),
        Vistoria.objects.filter(alocacao__veiculo=veiculo),
        Manutencao.objects.filter(veiculo=veiculo),
    ):
        candidatos.append(consulta.aggregate(m=Max("km"))["m"])
    return max(km for km in candidatos if km is not None)


def _ajustar_km_atual(veiculo, km_errado):
    """Baixa o KM atual do carro se ele veio da leitura errada. Devolve o aviso.

    Só mexe quando o KM atual é exatamente o da leitura corrigida/apagada — se
    outra fonte (devolução, manutenção, cadastro) já o levou além, ela manda.
    """
    veiculo.refresh_from_db(fields=["km_atual"])
    if veiculo.km_atual != km_errado:
        return None
    novo = km_atual_recalculado(veiculo)
    if novo == veiculo.km_atual:
        if RegistroKm.objects.filter(veiculo=veiculo, km=novo).exists():
            return None  # a leitura continua com esse número (só mudou data/observação)
        # Outro lançamento (devolução, vistoria, manutenção) tem o mesmo número —
        # provavelmente copiado da leitura errada. Sem mexer, mas a pessoa precisa saber.
        return (
            f"O KM atual de {veiculo.placa} continua {milhar(novo)} km porque outro lançamento "
            f"(devolução, vistoria ou manutenção) tem esse número. Se ele também estiver "
            f"errado, corrija-o na tela dele."
        )
    veiculo.km_atual = novo
    veiculo.save(update_fields=["km_atual"])
    return f"O KM atual de {veiculo.placa} voltou para {milhar(novo)} km."


def _cobranca_intocavel(cobranca):
    return cobranca.status == Cobranca.Status.JUDICIAL or cobranca.total_quitado > ZERO


def _cancelar(cobranca, motivo):
    cobranca.status = Cobranca.Status.CANCELADA
    cobranca.observacoes = f"{cobranca.observacoes}\n{motivo}".strip()
    cobranca.save(update_fields=["status", "observacoes"])


def refazer_excedente(registro):
    """Recalcula a cobrança de excedente da leitura depois de uma correção.

    Em aberto e sem pagamento: cancela e gera de novo com o KM certo. Com
    pagamento ou em judicial: não mexe e avisa. Cancelada pelo dono: respeita
    a decisão de não cobrar. Devolve o aviso para a tela (ou None).
    """
    rotulo = f"{registro.veiculo.placa} {registro.mes_referencia:%m/%Y}"
    cobranca = registro.cobranca_excedente
    if cobranca is not None and cobranca.status == Cobranca.Status.CANCELADA:
        return None
    if cobranca is not None and _cobranca_intocavel(cobranca):
        return (
            f"A leitura {rotulo} tem cobrança de excedente de {brl(cobranca.valor)} já paga "
            f"(ou em cobrança judicial) — ela não foi mexida. Confira no Financeiro se o "
            f"valor ainda está certo."
        )
    if cobranca is not None:
        _cancelar(cobranca, "Cancelada: a leitura de KM que a gerou foi corrigida.")
        registro.cobranca_excedente = None
        registro.save(update_fields=["cobranca_excedente"])
    nova = gerar_cobranca_excedente(registro)
    if cobranca is None:
        if nova is None:
            return None
        return (
            f"Com o KM corrigido, a leitura {rotulo} passou da franquia — cobrança de "
            f"excedente de {brl(nova.valor)} gerada para {nova.cliente.nome}."
        )
    if nova is None:
        return (
            f"A cobrança de excedente de {brl(cobranca.valor)} ({rotulo}) foi cancelada: "
            f"com o KM corrigido não há excedente."
        )
    if nova.valor == cobranca.valor:
        return (
            f"A cobrança de excedente ({rotulo}) foi refeita com o KM corrigido e continua "
            f"{brl(nova.valor)}."
        )
    return (
        f"A cobrança de excedente ({rotulo}) foi refeita com o KM corrigido: era "
        f"{brl(cobranca.valor)}, agora é {brl(nova.valor)}."
    )


def corrigir_leitura(registro, km_errado):
    """Grava a leitura já alterada (e validada) e refaz o que dependia dela.

    `km_errado` é o KM de antes da correção. Devolve a lista de avisos.
    """
    antes = RegistroKm.objects.filter(pk=registro.pk).values_list("km", "data_leitura").first()
    mudou_a_conta = antes != (registro.km, registro.data_leitura)
    with transaction.atomic():
        registro.save()
        avisos = []
        # Só a observação mudou: o excedente continua o mesmo — refazê-lo cancelaria
        # e recriaria a cobrança (vencimento novo) à toa.
        if mudou_a_conta:
            avisos.append(refazer_excedente(registro))
            seguinte = registro.seguinte_reencadeado
            if seguinte is not None:
                avisos.append(refazer_excedente(seguinte))
        avisos.append(_ajustar_km_atual(registro.veiculo, km_errado))
    return [aviso for aviso in avisos if aviso]


def apagar_leitura(registro):
    """Apaga a leitura, reapoia o mês seguinte e refaz excedente e KM atual."""
    cobranca = registro.cobranca_excedente
    if (
        cobranca is not None
        and cobranca.status != Cobranca.Status.CANCELADA
        and _cobranca_intocavel(cobranca)
    ):
        raise ValidationError(
            f"Esta leitura gerou cobrança de excedente de {brl(cobranca.valor)} que já tem "
            f"pagamento (ou está em cobrança judicial) — não dá para apagar. Corrija o número "
            f"em vez de apagar."
        )
    avisos = []
    with transaction.atomic():
        veiculo, km_errado = registro.veiculo, registro.km
        seguinte = registro.registro_seguinte()
        if cobranca is not None and cobranca.status != Cobranca.Status.CANCELADA:
            _cancelar(cobranca, "Cancelada: a leitura de KM que a gerou foi apagada.")
            avisos.append(f"A cobrança de excedente de {brl(cobranca.valor)} foi cancelada.")
        registro.delete()
        if seguinte is not None:
            seguinte.save()  # recalcula KM ANT/dias a partir da leitura que sobrou antes
            avisos.append(refazer_excedente(seguinte))
        avisos.append(_ajustar_km_atual(veiculo, km_errado))
    return [aviso for aviso in avisos if aviso]
