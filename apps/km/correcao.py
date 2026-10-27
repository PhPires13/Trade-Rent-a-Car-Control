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

from .excedente import gerar_cobrancas_excedente
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


def _fontes_de_km():
    """(rótulo, modelo, campo do carro, campo do KM, campo da data) de cada lançamento."""
    from apps.alocacoes.models import Alocacao, TrocaTemporaria, Vistoria
    from apps.manutencao.models import Manutencao

    return [
        ("manutenção", Manutencao, "veiculo_id", "km", "data"),
        ("vistoria", Vistoria, "veiculo_id", "km", "data"),
        ("entrega", Alocacao, "veiculo_id", "km_entrega", "data_inicio"),
        ("devolução", Alocacao, "veiculo_id", "km_devolucao", "data_termino"),
        (
            "retirada do substituto",
            TrocaTemporaria,
            "veiculo_substituto_id",
            "km_retirada",
            "data_retirada",
        ),
        (
            "devolução do substituto",
            TrocaTemporaria,
            "veiculo_substituto_id",
            "km_devolucao",
            "data_devolucao",
        ),
        ("leitura mensal", RegistroKm, "veiculo_id", "km", "data_leitura"),
    ]


def origens_do_km(kms_por_carro):
    """{id do carro: (rótulo, data)} para vários carros de uma vez ({id: km}).

    Uma consulta por tipo de lançamento, não por carro (a lista de KM tem a frota toda).
    """
    from django.db.models import Q

    achados = {}
    if not kms_por_carro:
        return {}
    for rotulo, modelo, campo_carro, campo_km, campo_data in _fontes_de_km():
        filtro = Q()
        for veiculo_id, km in kms_por_carro.items():
            filtro |= Q(**{campo_carro: veiculo_id, campo_km: km})
        linhas = (
            modelo.objects.filter(filtro)
            .filter(**{f"{campo_data}__isnull": False})
            .values_list(campo_carro, campo_data)
        )
        for veiculo_id, data in linhas:
            atual = achados.get(veiculo_id)
            if atual is None or data > atual[1]:
                achados[veiculo_id] = (rotulo, data)
    return {veiculo_id: achados.get(veiculo_id, ("cadastro", None)) for veiculo_id in kms_por_carro}


def origem_do_km(veiculo, km):
    """(rótulo, data) do lançamento mais recente que marcou este KM no carro.

    Ex.: ("manutenção", 02/10/2026). Sem lançamento com o número: ("cadastro", None).
    """
    return origens_do_km({veiculo.pk: km})[veiculo.pk]


def texto_da_origem(rotulo, data):
    """ "manutenção de 02/10" / "cadastro do carro" — para os avisos e a lista de KM."""
    if data is None:
        return "cadastro do carro"
    return f"{rotulo} de {data:%d/%m}"


def _menor_que_o_km_atual(registro):
    """Aviso quando a leitura nova é menor que o KM que o carro já marcou em outro
    lançamento (manutenção, devolução, vistoria...). None se não for o caso.

    Só para a leitura mais recente: num mês esquecido (há leitura depois) o
    número menor é o esperado. Na correção, o KM atual pode ser o da própria
    leitura errada — compara com o maior KM dos outros lançamentos.
    """
    veiculo = registro.veiculo
    corrigindo = registro.pk is not None
    km_base = km_atual_recalculado(veiculo, registro) if corrigindo else veiculo.km_atual
    if registro.km >= km_base:
        return None
    if registro.registro_seguinte() is not None:
        return None
    rotulo, data = origem_do_km(veiculo, km_base)
    if data is not None and data > registro.data_leitura:
        return None  # o lançamento com o KM maior é de depois desta leitura: normal
    if data is None:
        if corrigindo:
            return None  # só o cadastro: suspeita_do_km já compara com o KM de compra
        return (
            f"Você digitou {milhar(registro.km)} km, mas o cadastro do carro está com "
            f"{milhar(km_base)} km (o número é menor). Confira o painel. Confirma?"
        )
    return (
        f"Você digitou {milhar(registro.km)} km, mas o carro já marcou "
        f"{milhar(km_base)} km ({texto_da_origem(rotulo, data)}). "
        f"Confira o painel. Confirma?"
    )


def _salto_da_primeira_leitura(registro, cadastro):
    """1ª leitura com um zero a mais num carro de KM baixo (1.000 → 10.000)?

    Desconfia de 10 vezes o cadastro ou mais (carro zero, com poucos km no
    cadastro, fica de fora: nele qualquer número é muitas vezes o cadastro) e,
    com a data de onde veio o KM base (entrega, compra...), da mesma régua de
    LIMITE_KM_POR_DIA das outras leituras.
    """
    rodado = registro.km - cadastro
    if rodado <= KM_MINIMO_PARA_COMPARAR_MEDIA:
        return False
    if cadastro >= KM_MINIMO_PARA_COMPARAR_MEDIA and registro.km >= 10 * cadastro:
        return True
    veiculo = registro.veiculo
    _rotulo, data = origem_do_km(veiculo, cadastro)
    if data is None:
        data = veiculo.data_aquisicao
    if data is None or data > registro.data_leitura:
        return False
    dias = (registro.data_leitura - data).days
    return rodado > LIMITE_KM_POR_DIA * max(dias, 1)


def _maior_km_conhecido(registro, anterior):
    """ " e já marcou X km (manutenção de 16/10)" — o mesmo número da "Última" da
    linha, quando outro lançamento passou da leitura mensal anterior."""
    if registro.pk is not None:
        return ""  # na correção o KM atual pode ser o da própria leitura errada
    veiculo = registro.veiculo
    if veiculo.km_atual <= anterior.km or veiculo.km_atual >= registro.km:
        return ""
    rotulo, data = origem_do_km(veiculo, veiculo.km_atual)
    if data is None or data > registro.data_leitura:
        return ""
    return f" e já marcou {milhar(veiculo.km_atual)} km ({texto_da_origem(rotulo, data)})"


def suspeita_do_km(registro):
    """Texto do aviso quando o KM digitado foge do normal; None se está ok.

    Espera o registro já validado (full_clean) — KM menor que a leitura
    anterior é bloqueado lá, não chega aqui. KM menor que o KM atual do carro
    (vindo de manutenção, devolução, vistoria) também avisa.
    """
    menor = _menor_que_o_km_atual(registro)
    if menor:
        return menor
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
        if registro.km - cadastro > SALTO_MAXIMO_SEM_LEITURA or _salto_da_primeira_leitura(
            registro, cadastro
        ):
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
        f"(em {anterior.data_leitura:%d/%m/%Y}){_maior_km_conhecido(registro, anterior)}."
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


def km_atual_recalculado(veiculo, sem_registro=None):
    """Maior odômetro conhecido do carro em todos os lançamentos do sistema.

    `sem_registro`: leitura mensal que fica de fora (a que está sendo corrigida).
    """
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
    leituras = RegistroKm.objects.filter(veiculo=veiculo)
    if sem_registro is not None:
        leituras = leituras.exclude(pk=sem_registro.pk)
    for consulta in (
        leituras,
        # D10: a vistoria diz o carro vistoriado (o substituto, durante a troca)
        Vistoria.objects.filter(veiculo=veiculo),
        Manutencao.objects.filter(veiculo=veiculo),
    ):
        candidatos.append(consulta.aggregate(m=Max("km"))["m"])
    return max(km for km in candidatos if km is not None)


def _km_do_cadastro_antes(veiculo, km_errado):
    """Último KM atual do histórico do carro menor que o número errado (0 se não houver)."""
    anterior = (
        veiculo.history.filter(km_atual__lt=km_errado)
        .order_by("-history_date")
        .values_list("km_atual", flat=True)
        .first()
    )
    return anterior or 0


def ajustar_km_atual(veiculo, km_errado):
    """Baixa o KM atual do carro se ele veio da leitura errada. Devolve o aviso.

    Só mexe quando o KM atual é exatamente o da leitura corrigida/apagada — se
    outra fonte (devolução, manutenção, cadastro) já o levou além, ela manda.
    """
    veiculo.refresh_from_db(fields=["km_atual", "km_compra"])
    if veiculo.km_atual != km_errado:
        return None
    novo = km_atual_recalculado(veiculo)
    if novo == (veiculo.km_compra or 0):
        # Nenhum lançamento além do cadastro: volta ao KM que o cadastro tinha antes
        # do número errado (senão um carro sem KM de compra iria para 0 km).
        novo = max(novo, _km_do_cadastro_antes(veiculo, km_errado))
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


def _desfazer_acertos(registro, rotulo):
    """Cancela os acertos de excedente (cliente que devolveu antes) em aberto
    para serem refeitos. Devolve (avisos, {alocacao_id: cobrança cancelada}, pks
    de alocação que ficam como estão)."""
    avisos, cancelados, mantidos = [], {}, set()
    for acerto in registro.acertos_excedente.select_related("cobranca__cliente"):
        cobranca = acerto.cobranca
        mantidos.add(acerto.alocacao_id)
        if cobranca.status == Cobranca.Status.CANCELADA:
            continue  # o dono decidiu não cobrar
        if _cobranca_intocavel(cobranca):
            avisos.append(
                f"O excedente de {brl(cobranca.valor)} de {cobranca.cliente.nome} ({rotulo}) "
                f"já tem pagamento (ou está em cobrança judicial) — não foi mexido. Confira "
                f"no Financeiro se o valor ainda está certo."
            )
            continue
        _cancelar(cobranca, "Cancelada: a leitura de KM que a gerou foi corrigida.")
        acerto.delete()
        mantidos.discard(acerto.alocacao_id)
        cancelados[acerto.alocacao_id] = cobranca
    return avisos, cancelados, mantidos


def _avisos_dos_acertos(registro, rotulo, cancelados, mantidos):
    """Compara os acertos refeitos com os cancelados — texto do que mudou."""
    avisos = []
    novos = {
        acerto.alocacao_id: acerto.cobranca
        for acerto in registro.acertos_excedente.select_related("cobranca__cliente")
        if acerto.alocacao_id not in mantidos
    }
    for alocacao_id, antiga in cancelados.items():
        nova = novos.pop(alocacao_id, None)
        nome = antiga.cliente.nome
        if nova is None:
            avisos.append(
                f"A cobrança de excedente de {brl(antiga.valor)} de {nome} ({rotulo}) foi "
                f"cancelada: com o KM corrigido não há excedente."
            )
        elif nova.valor != antiga.valor:
            avisos.append(
                f"O excedente de {nome} ({rotulo}) foi refeito: era {brl(antiga.valor)}, "
                f"agora é {brl(nova.valor)}."
            )
    for nova in novos.values():
        avisos.append(
            f"Com o KM corrigido, {nova.cliente.nome} passou da franquia em {rotulo} — "
            f"cobrança de excedente de {brl(nova.valor)} gerada."
        )
    return avisos


def refazer_excedente(registro):
    """Recalcula a cobrança de excedente da leitura depois de uma correção.

    Em aberto e sem pagamento: cancela e gera de novo com o KM certo. Com
    pagamento ou em judicial: não mexe e avisa. Cancelada pelo dono: respeita
    a decisão de não cobrar. Devolve o aviso para a tela (ou None). Os
    acertos de quem devolveu o carro antes (C15) seguem a mesma regra; o
    aviso deles vai junto no mesmo texto.
    """
    rotulo = f"{registro.veiculo.placa} {registro.mes_referencia:%m/%Y}"
    avisos, cancelados, mantidos = _desfazer_acertos(registro, rotulo)
    principal = _refazer_principal(registro, rotulo)
    avisos += _avisos_dos_acertos(registro, rotulo, cancelados, mantidos)
    textos = [texto for texto in [principal, *avisos] if texto]
    return " ".join(textos) or None


def _refazer_principal(registro, rotulo):
    cobranca = registro.cobranca_excedente
    if cobranca is not None and cobranca.status == Cobranca.Status.CANCELADA:
        gerar_cobrancas_excedente(registro)  # refaz só os acertos
        return None
    if cobranca is not None and _cobranca_intocavel(cobranca):
        gerar_cobrancas_excedente(registro)  # refaz só os acertos
        return (
            f"A leitura {rotulo} tem cobrança de excedente de {brl(cobranca.valor)} já paga "
            f"(ou em cobrança judicial) — ela não foi mexida. Confira no Financeiro se o "
            f"valor ainda está certo."
        )
    if cobranca is not None:
        _cancelar(cobranca, "Cancelada: a leitura de KM que a gerou foi corrigida.")
        registro.cobranca_excedente = None
        registro.save(update_fields=["cobranca_excedente"])
    gerar_cobrancas_excedente(registro)
    nova = registro.cobranca_excedente
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
        avisos.append(ajustar_km_atual(registro.veiculo, km_errado))
    return [aviso for aviso in avisos if aviso]


#: nome antigo, mantido para quem já importava a função privada
_ajustar_km_atual = ajustar_km_atual


def apagar_leitura(registro):
    """Apaga a leitura, reapoia o mês seguinte e refaz excedente e KM atual."""
    cobranca = registro.cobranca_excedente
    acertos = [
        acerto.cobranca
        for acerto in registro.acertos_excedente.select_related("cobranca")
        if acerto.cobranca.status != Cobranca.Status.CANCELADA
    ]
    if any(_cobranca_intocavel(acerto) for acerto in acertos):
        raise ValidationError(
            "Esta leitura gerou cobrança de excedente que já tem pagamento (ou está em "
            "cobrança judicial) — não dá para apagar. Corrija o número em vez de apagar."
        )
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
        for acerto in acertos:
            _cancelar(acerto, "Cancelada: a leitura de KM que a gerou foi apagada.")
            avisos.append(
                f"A cobrança de excedente de {brl(acerto.valor)} de {acerto.cliente.nome} "
                f"foi cancelada."
            )
        registro.delete()
        if seguinte is not None:
            seguinte.save()  # recalcula KM ANT/dias a partir da leitura que sobrou antes
            avisos.append(refazer_excedente(seguinte))
        avisos.append(ajustar_km_atual(veiculo, km_errado))
    return [aviso for aviso in avisos if aviso]
