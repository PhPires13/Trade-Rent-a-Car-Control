"""Entrega e devolução guiadas: o que já foi feito e o que falta (proposta #15).

Entrega: depois de alocar, a ficha do cliente mostra um cartão com os passos
(contrato, vistoria de entrega, caução, 1ª semana), cada um com seu botão e um
✓ que se marca sozinho conforme a pessoa faz. Devolução: a tela de encerrar
mostra os passos (vistoria de devolução → KM final → caução → confirmar).
"""

from datetime import date, timedelta
from urllib.parse import urlencode

from django.db.models import Max, Q
from django.urls import reverse

from apps.formatos import brl, milhar

from .models import Alocacao, Vistoria


def _url(nome, *args, **query):
    url = reverse(nome, args=args)
    return f"{url}?{urlencode(query)}" if query else url


def _caucao(alocacao):
    from apps.financeiro.models import Caucao  # import local evita ciclo de import

    return Caucao.com_saldos_anotados(Caucao.objects.filter(alocacao=alocacao)).first()


def _vistorias_do_carro_da_locacao(alocacao):
    """D10: vistoria do substituto não conta como entrega/devolução do carro da locação."""
    return alocacao.vistorias.filter(Q(veiculo_id=alocacao.veiculo_id) | Q(veiculo__isnull=True))


def passos_da_entrega(alocacao):
    """Passos da entrega do carro, ou None quando o cartão não deve aparecer.

    O cartão some quando a alocação foi encerrada, quando a pessoa dispensou
    ou quando tudo já foi feito.
    """
    if (
        alocacao is None
        or alocacao.status != Alocacao.Status.ATIVA
        or alocacao.passos_entrega_dispensados
    ):
        return None
    from apps.financeiro.models import Cobranca  # import local evita ciclo de import

    ficha = reverse("pessoas:detalhe", args=[alocacao.cliente_id])
    passos = []

    passos.append(
        {
            "titulo": "Imprimir o contrato",
            "feito": alocacao.contrato_aberto_em is not None,
            "detalhe": (
                f"Aberto para imprimir em {alocacao.contrato_aberto_em:%d/%m/%Y}."
                if alocacao.contrato_aberto_em
                else "Imprima duas vias e colha a assinatura do cliente."
            ),
            "url": _url("alocacoes:contrato", alocacao.pk, voltar=ficha),
            "botao": "Abrir contrato",
        }
    )

    vistoria = (
        _vistorias_do_carro_da_locacao(alocacao)
        .filter(tipo=Vistoria.Tipo.ENTRADA)
        .order_by("-data", "-pk")
        .first()
    )
    passos.append(
        {
            "titulo": "Fazer a vistoria de entrega",
            "feito": vistoria is not None,
            "detalhe": (
                f"Feita em {vistoria.data:%d/%m/%Y}"
                + (f" — {milhar(vistoria.km)} km." if vistoria.km else ".")
                if vistoria
                else "Anote marcas, combustível e KM antes do carro sair."
            ),
            "url": _url("alocacoes:vistoria_nova", alocacao.pk, tipo="entrada", voltar=ficha),
            "botao": "Registrar vistoria",
        }
    )

    if alocacao.caucao_valor:
        caucao = _caucao(alocacao)
        recebido = caucao.recebido if caucao else 0
        # Caução paga em partes só conta como feita quando o valor combinado entrou.
        if recebido >= alocacao.caucao_valor:
            detalhe = f"Recebido: {brl(recebido)}."
        elif recebido > 0:
            detalhe = (
                f"Recebido {brl(recebido)} de {brl(alocacao.caucao_valor)} — "
                f"faltam {brl(alocacao.caucao_valor - recebido)}."
            )
        else:
            detalhe = "Registre o depósito da caução quando o cliente pagar."
        passos.append(
            {
                "titulo": f"Receber a caução ({brl(alocacao.caucao_valor)})",
                "feito": recebido >= alocacao.caucao_valor,
                "detalhe": detalhe,
                "url": (
                    reverse("financeiro:caucao_detalhe", args=[caucao.pk])
                    if caucao
                    else reverse("financeiro:caucoes")
                ),
                "botao": "Registrar caução",
            }
        )

    primeira = (
        Cobranca.com_quitacao_anotada(
            Cobranca.objects.filter(alocacao=alocacao, origem=Cobranca.Origem.ALUGUEL)
        )
        .exclude(status=Cobranca.Status.CANCELADA)
        .order_by("vencimento")
        .first()
    )
    paga = primeira is not None and primeira.status == Cobranca.Status.PAGO
    if paga:
        detalhe = f"Paga — vencimento {primeira.vencimento:%d/%m/%Y}."
    elif primeira and primeira.saldo < primeira.valor:
        detalhe = (
            f"Pago em parte: faltam {brl(primeira.saldo)} de {brl(primeira.valor)} "
            f"(vencimento {primeira.vencimento:%d/%m/%Y})."
        )
    elif primeira:
        detalhe = f"{brl(primeira.valor)} com vencimento em {primeira.vencimento:%d/%m/%Y}."
    else:
        detalhe = (
            f"A cobrança de {brl(alocacao.valor_semanal)} aparece no dia do início "
            f"({alocacao.data_inicio:%d/%m/%Y})."
        )
    passos.append(
        {
            "titulo": "Receber a 1ª semana",
            "feito": paga,
            "detalhe": detalhe,
            "url": _url("financeiro:receber", cliente=alocacao.cliente_id),
            "botao": "Receber pagamento",
        }
    )

    if all(passo["feito"] for passo in passos):
        return None
    return passos


#: Vistoria de devolução com mais dias que isso não sugere a data do encerramento.
DIAS_DATA_VISTORIA = 3


def vistoria_de_devolucao(alocacao, vistorias=None):
    """A vistoria de devolução que vale para encerrar, ou None.

    Só a vistoria mais recente do carro da locação, quando é de devolução e não
    veio antes da última devolução de substituto — uma "devolução" antiga (o
    carro foi para a oficina e voltou ao motorista) não pode marcar o passo
    como feito nem sugerir data e KM.
    """
    if vistorias is None:
        vistorias = list(_vistorias_do_carro_da_locacao(alocacao).order_by("-data", "-pk"))
    if not vistorias or vistorias[0].tipo != Vistoria.Tipo.SAIDA:
        return None
    ultima = vistorias[0]
    fim_troca = alocacao.trocas.aggregate(m=Max("data_devolucao"))["m"]
    if fim_troca and ultima.data < fim_troca:
        return None
    return ultima


def situacao_da_devolucao(alocacao):
    """O que a tela de encerrar mostra em passos: vistorias, débitos e caução."""
    from apps.financeiro.models import Cobranca  # import local evita ciclo de import

    vistorias = list(_vistorias_do_carro_da_locacao(alocacao).order_by("-data", "-pk"))
    entrega = next((v for v in vistorias if v.tipo == Vistoria.Tipo.ENTRADA), None)
    devolucao = vistoria_de_devolucao(alocacao, vistorias)
    hoje = date.today()
    # Vistoria de devolução antiga (o carro foi conferido e o motorista seguiu com
    # ele) não serve de data: sugere hoje e a pessoa confere.
    data_da_vistoria = bool(
        devolucao and devolucao.data >= hoje - timedelta(days=DIAS_DATA_VISTORIA)
    )
    data_devolucao = devolucao.data if data_da_vistoria else hoje
    # D03: semanas que começam no dia da devolução (ou depois) são canceladas ao
    # encerrar — não são dívida, e mandar receber agora faria pagar o que não usou
    devidas = Cobranca.com_quitacao_anotada(
        Cobranca.objects.filter(cliente_id=alocacao.cliente_id, status__in=Cobranca.STATUS_DEVIDOS)
    )
    debitos = 0
    semanas_a_cancelar = []
    for cobranca in devidas:
        if (
            cobranca.alocacao_id == alocacao.pk
            and cobranca.origem == Cobranca.Origem.ALUGUEL
            and cobranca.status != Cobranca.Status.JUDICIAL
            and (
                cobranca.vencimento >= data_devolucao
                # semana de transição (D04): mesma regra do encerrar
                or (cobranca.periodo_inicio and cobranca.periodo_inicio >= data_devolucao)
            )
        ):
            semanas_a_cancelar.append(cobranca)
        else:
            debitos += cobranca.saldo
    return {
        "vistoria_entrega": entrega,
        "vistoria_devolucao": devolucao,
        "data_sugerida": data_devolucao,
        "data_da_vistoria": data_da_vistoria,
        "debitos": debitos,
        "semanas_a_cancelar": semanas_a_cancelar,
        "url_vistoria_devolucao": _url(
            "alocacoes:vistoria_nova",
            alocacao.pk,
            tipo="saida",
            voltar=reverse("alocacoes:encerrar", args=[alocacao.pk]),
        ),
    }
