from datetime import date, timedelta

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.frota.models import Veiculo

from .excedente import gerar_cobranca_excedente
from .models import RegistroKm, primeiro_dia_do_mes, veiculos_com_leitura_pendente


def _mes_do_valor(valor):
    """Converte "AAAA-MM" em 1º do mês; None quando vier vazio ou torto."""
    try:
        ano, mes = (valor or "").split("-")
        return date(int(ano), int(mes), 1)
    except (ValueError, TypeError):
        return None


def _mes_da_query(request):
    return _mes_do_valor(request.GET.get("mes")) or primeiro_dia_do_mes(date.today())


def _data_sugerida(mes, hoje):
    """Data que o formulário de registro propõe para o mês que está na tela.

    No mês corrente é hoje; num mês já fechado é o último dia dele — senão a
    leitura cai no mês corrente e a pendência do mês visto continua lá.
    """
    ultimo_dia = primeiro_dia_do_mes(mes + timedelta(days=32)) - timedelta(days=1)
    return min(hoje, ultimo_dia)


def _volta_para_lista(mes):
    """Devolve a lista no mês que a pessoa estava vendo, não no mês corrente."""
    if mes is None:
        return redirect("km:lista")
    return redirect(f"{reverse('km:lista')}?mes={mes:%Y-%m}")


def _clientes_das_leituras(registros):
    """{registro.pk: cliente na data da leitura} em 2 queries — mesma regra de
    cliente_vigente (docs.md §4.2: quem estava com o carro no dia, inclusive
    como substituto), só que as trocas e alocações do período vêm de uma vez.
    """
    if not registros:
        return {}
    veiculos = {registro.veiculo_id for registro in registros}
    datas = [registro.data_leitura for registro in registros]
    primeira, ultima = min(datas), max(datas)

    trocas, alocacoes = {}, {}
    for troca in (
        TrocaTemporaria.objects.filter(
            veiculo_substituto_id__in=veiculos, data_retirada__lte=ultima
        )
        .filter(Q(data_devolucao__isnull=True) | Q(data_devolucao__gte=primeira))
        .select_related("alocacao__cliente")
        .order_by("-data_retirada")
    ):
        trocas.setdefault(troca.veiculo_substituto_id, []).append(troca)
    for alocacao in (
        Alocacao.objects.filter(veiculo_id__in=veiculos, data_inicio__lte=ultima)
        .filter(Q(data_termino__isnull=True) | Q(data_termino__gte=primeira))
        .select_related("cliente")
        .order_by("-data_inicio")
    ):
        alocacoes.setdefault(alocacao.veiculo_id, []).append(alocacao)

    clientes = {}
    for registro in registros:
        dia = registro.data_leitura
        troca = next(
            (
                t
                for t in trocas.get(registro.veiculo_id, ())
                if t.data_retirada <= dia and (t.data_devolucao is None or t.data_devolucao >= dia)
            ),
            None,
        )
        if troca:
            clientes[registro.pk] = troca.alocacao.cliente
            continue
        alocacao = next(
            (
                a
                for a in alocacoes.get(registro.veiculo_id, ())
                if a.data_inicio <= dia and (a.data_termino is None or a.data_termino >= dia)
            ),
            None,
        )
        clientes[registro.pk] = alocacao.cliente if alocacao else None
    return clientes


def lista_mensal(request):
    """Registro mensal de KM — pendências e leituras do mês (docs.md §4.8)."""
    mes = _mes_da_query(request)
    registros = list(
        RegistroKm.objects.filter(mes_referencia=mes)
        .select_related("veiculo", "cobranca_excedente")
        .order_by("veiculo__placa")
    )
    clientes = _clientes_das_leituras(registros)
    for registro in registros:
        registro.cliente = clientes[registro.pk]
    pendentes = veiculos_com_leitura_pendente(mes)
    return render(
        request,
        "km/lista.html",
        {
            "mes": mes,
            "registros": registros,
            "pendentes": pendentes,
            "data_sugerida": _data_sugerida(mes, date.today()),
        },
    )


def _avisar_reencadeamento(request, veiculo, registro):
    """Leitura de mês esquecido reapoia o mês seguinte — avisa o que mudou."""
    seguinte = registro.seguinte_reencadeado
    if seguinte is None:
        return
    aviso = (
        f"A leitura de {seguinte.mes_referencia:%m/%Y} de {veiculo.placa} foi recalculada: "
        f"KM anterior agora é {registro.km} e o KM utilizado dela caiu para "
        f"{seguinte.km_utilizado}."
    )
    if seguinte.cobranca_excedente_id:
        aviso += (
            " Ela já tinha cobrança de excedente de km — confira no Financeiro e cancele "
            "se ficou em duplicidade."
        )
    messages.warning(request, aviso)


def registrar(request, veiculo_id):
    veiculo = get_object_or_404(Veiculo, pk=veiculo_id)
    if request.method != "POST":
        return redirect("km:lista")
    # O mês visto na tela vem escondido no formulário da linha; se faltar, o
    # mês da própria leitura serve de volta.
    mes_da_tela = _mes_do_valor(request.POST.get("mes"))
    try:
        data_leitura = date.fromisoformat(request.POST["data_leitura"])
        registro = RegistroKm(
            veiculo=veiculo,
            mes_referencia=primeiro_dia_do_mes(data_leitura),
            data_leitura=data_leitura,
            km=int(request.POST["km"]),
            observacoes=request.POST.get("observacoes", ""),
        )
        registro.full_clean()
        registro.save()
        mes_da_tela = mes_da_tela or registro.mes_referencia
        messages.success(request, f"KM de {veiculo.placa} registrado: {registro.km} km.")
        _avisar_reencadeamento(request, veiculo, registro)
        cobranca = gerar_cobranca_excedente(registro)
        if cobranca:
            messages.warning(
                request,
                f"Franquia estourada — cobrança de excedente de R$ {cobranca.valor} "
                f"gerada para {cobranca.cliente.nome} (cancele no Financeiro se não for cobrar).",
            )
    except ValidationError as erro:
        messages.error(request, f"{veiculo.placa}: {'; '.join(erro.messages)}")
    except (KeyError, ValueError):
        messages.error(request, f"{veiculo.placa}: preencha a data e o KM corretamente.")
    return _volta_para_lista(mes_da_tela)


def historico(request, veiculo_id):
    veiculo = get_object_or_404(Veiculo, pk=veiculo_id)
    registros = veiculo.registros_km.order_by("-mes_referencia")
    return render(request, "km/historico.html", {"veiculo": veiculo, "registros": registros})
