from datetime import date

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import OuterRef, Q, Subquery
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.formatos import brl, milhar
from apps.frota.models import Veiculo

from .correcao import apagar_leitura, corrigir_leitura, suspeita_do_km
from .excedente import gerar_cobrancas_excedente
from .forms import CorrecaoLeituraForm, data_nao_futura, km_digitado
from .models import (
    RegistroKm,
    leitura_cabe_no_mes,
    mes_da_leitura,
    mes_padrao_da_lista,
    primeiro_dia_do_mes,
    veiculos_com_leitura_pendente,
)

#: Anos aceitos no ?mes= — fora disso é endereço torto (9999-12 dava erro 500).
ANO_MINIMO = 2000


def _mes_do_valor(valor, hoje=None):
    """Converte "AAAA-MM" em 1º do mês; None quando vier vazio, torto ou com
    ano fora de uma faixa razoável (de 2000 ao ano que vem)."""
    try:
        ano, mes = (valor or "").split("-")
        ano = int(ano)
        if not ANO_MINIMO <= ano <= (hoje or date.today()).year + 1:
            return None
        return date(ano, int(mes), 1)
    except (ValueError, TypeError):
        return None


def _mes_da_query(request):
    hoje = date.today()
    return _mes_do_valor(request.GET.get("mes"), hoje) or mes_padrao_da_lista(hoje)


def _data_sugerida(mes, hoje):
    """Data que o formulário de registro propõe para o mês que está na tela.

    No mês corrente (ou num mês futuro) é hoje. No mês anterior, durante os
    primeiros dias, também é hoje: é a leitura de fechamento dele (D21). Num
    mês já fechado fora disso não sugere nada (D19): a tela pede o KM anotado
    naquela data — o painel de hoje já tem o km do mês atual somado.
    """
    if mes >= primeiro_dia_do_mes(hoje) or leitura_cabe_no_mes(hoje, mes):
        return hoje
    return None


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


def _pendentes_anotados(mes, veiculos=None):
    """Pendentes do mês com a última leitura antes dele (dica do número esperado).

    `veiculos` troca a base (ex.: o carro de uma linha que já tem leitura no mês).
    """
    anteriores = RegistroKm.objects.filter(veiculo=OuterRef("pk"), mes_referencia__lt=mes).order_by(
        "-mes_referencia"
    )
    base = veiculos_com_leitura_pendente(mes) if veiculos is None else veiculos
    return base.annotate(
        ultimo_km=Subquery(anteriores.values("km")[:1]),
        ultima_leitura=Subquery(anteriores.values("data_leitura")[:1]),
    )


def _contexto_da_linha(mes):
    """O que cada linha de pendente precisa além do próprio veículo."""
    hoje = date.today()
    return {
        "mes": mes,
        "data_sugerida": _data_sugerida(mes, hoje),
        "hoje": hoje,
        # mês anterior nos primeiros dias: a leitura de hoje fecha ele (D21)
        "fechamento": mes < primeiro_dia_do_mes(hoje) and leitura_cabe_no_mes(hoje, mes),
    }


def lista_mensal(request):
    """Registro mensal de KM — pendências e leituras do mês (docs.md §4.8)."""
    return _pagina_da_lista(request, _mes_da_query(request))


def _pagina_da_lista(request, mes, estados=None):
    """`estados` ({veiculo.pk: {...}}) devolve a linha com o que foi digitado e o
    aviso de KM fora do normal, quando o registro não foi gravado.
    """
    registros = list(
        RegistroKm.objects.filter(mes_referencia=mes)
        .select_related("veiculo", "cobranca_excedente")
        .prefetch_related("acertos_excedente__cobranca")
        .order_by("veiculo__placa")
    )
    clientes = _clientes_das_leituras(registros)
    for registro in registros:
        registro.cliente = clientes[registro.pk]
    pendentes = list(_pendentes_anotados(mes))
    for veiculo in pendentes:
        veiculo.estado = (estados or {}).get(veiculo.pk)
    return render(
        request,
        "km/lista.html",
        {"registros": registros, "pendentes": pendentes, **_contexto_da_linha(mes)},
    )


def _aviso_reencadeamento(registro):
    """Leitura de mês esquecido reapoia o mês seguinte — texto do que mudou."""
    seguinte = registro.seguinte_reencadeado
    if seguinte is None:
        return None
    aviso = (
        f"A leitura de {seguinte.mes_referencia:%m/%Y} de {registro.veiculo.placa} foi "
        f"recalculada: KM anterior agora é {milhar(registro.km)} e o KM utilizado dela caiu "
        f"para {milhar(seguinte.km_utilizado)}."
    )
    if seguinte.cobranca_excedente_id:
        aviso += (
            " Ela já tinha cobrança de excedente de km — confira no Financeiro e cancele "
            "se ficou em duplicidade."
        )
    return aviso


def _linha_nao_gravada(request, veiculo, mes, estado):
    """Volta a linha com o que foi digitado e o aviso/erro — sem perder nada."""
    if request.htmx:
        # Sem o filtro de pendente: numa leitura repetida o carro já saiu da lista,
        # mas a linha ainda precisa da "Última: ..." para não ficar em branco.
        veiculo = _pendentes_anotados(mes, Veiculo.objects.filter(pk=veiculo.pk)).first()
        veiculo.estado = estado
        return render(request, "km/_pendente.html", {"veiculo": veiculo, **_contexto_da_linha(mes)})
    if estado.get("erro"):
        messages.error(request, f"{veiculo.placa}: {estado['erro']}")
        return _volta_para_lista(mes)
    # KM fora do normal sem JS: a página inteira volta com a linha em âmbar.
    return _pagina_da_lista(request, mes, estados={veiculo.pk: estado})


def registrar(request, veiculo_id):
    """Grava a leitura de uma linha de pendentes.

    Pelo htmx (celular no pátio) devolve só a linha: "✓ registrado" ou o aviso,
    sem recarregar a página. Sem htmx, volta para a lista como sempre.
    """
    veiculo = get_object_or_404(Veiculo, pk=veiculo_id)
    if request.method != "POST":
        return redirect("km:lista")
    request.htmx = request.headers.get("HX-Request") == "true"
    # O mês visto na tela vem escondido no formulário da linha; se faltar, o
    # mês da própria leitura serve de volta.
    mes_da_tela = _mes_do_valor(request.POST.get("mes"))
    estado = {
        "km": request.POST.get("km", ""),
        "data_leitura": request.POST.get("data_leitura", ""),
        "observacoes": request.POST.get("observacoes", ""),
    }
    try:
        data_leitura = date.fromisoformat(request.POST["data_leitura"])
        mes_da_tela = mes_da_tela or primeiro_dia_do_mes(data_leitura)
        registro = RegistroKm(
            veiculo=veiculo,
            # leitura de fechamento no dia 1º vale para o mês da tela (D21)
            mes_referencia=mes_da_leitura(data_leitura, mes_da_tela),
            data_leitura=data_nao_futura(data_leitura),
            km=km_digitado(request.POST["km"]),
            observacoes=estado["observacoes"],
        )
        existente = RegistroKm.objects.filter(
            veiculo=veiculo, mes_referencia=registro.mes_referencia
        ).first()
        if existente is not None:
            # Toque duplo ou duas abas: diz o que já está gravado e onde corrigir.
            raise ValidationError(
                f"{veiculo.placa} já tem leitura em {registro.mes_referencia:%m/%Y} "
                f"({milhar(existente.km)} km em {existente.data_leitura:%d/%m/%Y}). "
                f"Se o número está errado, use Corrigir em Leituras do mês."
            )
        registro.full_clean()
        aviso = suspeita_do_km(registro)
        # A caixinha vale só para o número avisado: trocou o KM, confere de novo.
        if aviso and request.POST.get("confirmo") != str(registro.km):
            estado["aviso"] = aviso
            estado["km_avisado"] = registro.km
            return _linha_nao_gravada(request, veiculo, mes_da_tela, estado)
        try:
            with transaction.atomic():
                registro.save()
        except IntegrityError:
            raise ValidationError(
                f"{veiculo.placa} acabou de ganhar leitura em {registro.mes_referencia:%m/%Y} "
                f"por outra tela. Recarregue a página para ver."
            ) from None
    except ValidationError as erro:
        estado["erro"] = "; ".join(erro.messages)
        return _linha_nao_gravada(
            request, veiculo, mes_da_tela or primeiro_dia_do_mes(date.today()), estado
        )
    except (KeyError, ValueError):
        estado["erro"] = "preencha a data e o KM (só números)."
        return _linha_nao_gravada(
            request, veiculo, mes_da_tela or primeiro_dia_do_mes(date.today()), estado
        )

    sucesso = f"KM de {veiculo.placa} registrado: {milhar(registro.km)} km"
    if registro.km_utilizado is not None:
        sucesso += f" (rodou {milhar(registro.km_utilizado)} km desde a leitura anterior)"
    avisos = [_aviso_reencadeamento(registro)]
    for cobranca in gerar_cobrancas_excedente(registro):
        avisos.append(
            f"Franquia estourada — cobrança de excedente de {brl(cobranca.valor)} "
            f"gerada para {cobranca.cliente.nome} (cancele no Financeiro se não for cobrar)."
        )
    avisos = [aviso for aviso in avisos if aviso]
    if request.htmx:
        return render(
            request,
            "km/_pendente_ok.html",
            {
                "registro": registro,
                "sucesso": sucesso,
                "avisos": avisos,
                "mes": mes_da_tela,
                "faltam": veiculos_com_leitura_pendente(mes_da_tela).count(),
            },
        )
    messages.success(request, f"{sucesso}.")
    for aviso in avisos:
        messages.warning(request, aviso)
    return _volta_para_lista(mes_da_tela)


def corrigir(request, registro_id):
    """Corrige ou apaga uma leitura digitada errada (proposta de UX nº 4, opção C)."""
    registro = get_object_or_404(RegistroKm.objects.select_related("veiculo"), pk=registro_id)
    veiculo = registro.veiculo
    voltar_para_lista = (request.POST.get("voltar") or request.GET.get("voltar")) == "lista"

    def voltar():
        if voltar_para_lista:
            return _volta_para_lista(registro.mes_referencia)
        return redirect("km:historico", veiculo.pk)

    aviso = None
    if request.method == "POST" and "apagar" in request.POST:
        try:
            avisos = apagar_leitura(registro)
        except ValidationError as erro:
            messages.error(request, "; ".join(erro.messages))
            return redirect(request.get_full_path())
        messages.success(
            request,
            f"Leitura de {registro.mes_referencia:%m/%Y} de {veiculo.placa} apagada "
            f"({milhar(registro.km)} km).",
        )
        for texto in avisos:
            messages.warning(request, texto)
        return voltar()

    km_errado = registro.km
    form = CorrecaoLeituraForm(request.POST or None, instance=registro)
    if request.method == "POST" and form.is_valid():
        aviso = suspeita_do_km(form.instance)
        if aviso is None or form.confirmou():
            avisos = corrigir_leitura(form.instance, km_errado)
            messages.success(
                request,
                f"Leitura de {registro.mes_referencia:%m/%Y} de {veiculo.placa} corrigida: "
                f"{milhar(km_errado)} → {milhar(registro.km)} km.",
            )
            for texto in avisos:
                messages.warning(request, texto)
            return voltar()
    return render(
        request,
        "km/corrigir.html",
        {
            "registro": registro,
            "veiculo": veiculo,
            "km_errado": km_errado,
            "form": form,
            "aviso": aviso,
            "km_avisado": form.cleaned_data.get("km") if aviso else None,
            "voltar_para_lista": voltar_para_lista,
            "cobranca": registro.cobranca_excedente,
        },
    )


def historico(request, veiculo_id):
    veiculo = get_object_or_404(Veiculo, pk=veiculo_id)
    registros = veiculo.registros_km.order_by("-mes_referencia")
    return render(request, "km/historico.html", {"veiculo": veiculo, "registros": registros})
