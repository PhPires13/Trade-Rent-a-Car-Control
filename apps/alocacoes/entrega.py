"""Entrega e devolução guiadas: o que já foi feito e o que falta (proposta #15).

Entrega: depois de alocar, a ficha do cliente mostra um cartão com os passos
(contrato, vistoria de entrega, caução, 1ª semana), cada um com seu botão e um
✓ que se marca sozinho conforme a pessoa faz. Devolução: a tela de encerrar
mostra os passos (vistoria de devolução → KM final → caução → confirmar).
"""

from urllib.parse import urlencode

from django.urls import reverse

from apps.formatos import brl, milhar

from .models import Alocacao, Vistoria


def _url(nome, *args, **query):
    url = reverse(nome, args=args)
    return f"{url}?{urlencode(query)}" if query else url


def _caucao(alocacao):
    from apps.financeiro.models import Caucao  # import local evita ciclo de import

    return Caucao.com_saldos_anotados(Caucao.objects.filter(alocacao=alocacao)).first()


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
            "url": reverse("alocacoes:contrato", args=[alocacao.pk]),
            "botao": "Abrir contrato",
        }
    )

    vistoria = (
        alocacao.vistorias.filter(tipo=Vistoria.Tipo.ENTRADA).order_by("-data", "-pk").first()
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


def situacao_da_devolucao(alocacao):
    """O que a tela de encerrar mostra em passos: vistorias, débitos e caução."""
    from apps.financeiro.models import Cobranca  # import local evita ciclo de import

    vistorias = list(alocacao.vistorias.order_by("-data", "-pk"))
    entrega = next((v for v in vistorias if v.tipo == Vistoria.Tipo.ENTRADA), None)
    devolucao = next((v for v in vistorias if v.tipo == Vistoria.Tipo.SAIDA), None)
    devidas = Cobranca.com_quitacao_anotada(
        Cobranca.objects.filter(cliente_id=alocacao.cliente_id, status__in=Cobranca.STATUS_DEVIDOS)
    )
    debitos = sum((cobranca.saldo for cobranca in devidas), start=0)
    return {
        "vistoria_entrega": entrega,
        "vistoria_devolucao": devolucao,
        "debitos": debitos,
        "url_vistoria_devolucao": _url(
            "alocacoes:vistoria_nova",
            alocacao.pk,
            tipo="saida",
            voltar=reverse("alocacoes:encerrar", args=[alocacao.pk]),
        ),
    }
