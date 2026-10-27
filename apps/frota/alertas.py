"""Vigências e documentos a vencer — IPVA, licenciamento, rastreador, bateria e CNH (§5)."""

from datetime import timedelta
from urllib.parse import quote

from django.db.models import Count, Min
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from apps.formatos import brl, milhar
from apps.pessoas.models import Cliente

from .models import PagamentoIpva, Veiculo

DIAS_ALERTA_VIGENCIA = 30

#: "ainda não buscado" — a tela que já sabe se o carro está na oficina passa o valor
_BUSCAR = object()


def _alerta(data, hoje, descricao, url=None):
    """`url`: a tela onde se resolve o alerta (o painel mostra a linha como link)."""
    return {"data": data, "vencido": data < hoje, "descricao": descricao, "url": url}


def vigencias_a_vencer(hoje=None):
    """Alertas vencidos ou vencendo nos próximos 30 dias, do mais antigo ao mais novo."""
    hoje = hoje or timezone.localdate()
    limite = hoje + timedelta(days=DIAS_ALERTA_VIGENCIA)
    alertas = []
    frota = Veiculo.objects.exclude(status=Veiculo.Status.VENDIDO)
    # IPVA em aberto (sem data de pagamento) vencendo — decisão nº 21 / tabela do §5
    for parcela in ipva_em_aberto(limite).select_related("veiculo"):
        alertas.append(
            _alerta(
                parcela.vencimento,
                hoje,
                f"{parcela.rotulo} do {parcela.veiculo.placa}"
                + (f" — {brl(parcela.valor)}" if parcela.valor else ""),
                reverse("frota:ipva", args=[parcela.veiculo_id]),
            )
        )
    # virada do ano: o carro tinha IPVA lançado em anos anteriores e o deste ano não
    for veiculo in sem_ipva_do_ano(hoje.year):
        alertas.append(
            _alerta(
                hoje,
                hoje,
                f"IPVA {hoje.year} do {veiculo.placa} ainda não cadastrado",
                reverse("frota:ipva", args=[veiculo.pk]),
            )
        )
    for veiculo in frota.filter(licenciamento_vencimento__lte=limite):
        alertas.append(
            _alerta(
                veiculo.licenciamento_vencimento,
                hoje,
                f"Licenciamento do {veiculo.placa}",
                reverse("frota:veiculo_editar", args=[veiculo.pk]),
            )
        )
    for veiculo in frota.filter(rastreador_vigencia_fim__lte=limite):
        alertas.append(
            _alerta(
                veiculo.rastreador_vigencia_fim,
                hoje,
                f"Rastreador do {veiculo.placa}"
                + (f" ({veiculo.rastreador_fornecedor})" if veiculo.rastreador_fornecedor else ""),
                reverse("frota:veiculo_editar", args=[veiculo.pk]),
            )
        )
    for veiculo in frota.filter(bateria_garantia_fim__lte=limite):
        alertas.append(
            _alerta(
                veiculo.bateria_garantia_fim,
                hoje,
                f"Garantia da bateria do {veiculo.placa}",
                reverse("frota:veiculo_editar", args=[veiculo.pk]),
            )
        )
    for cliente in Cliente.objects.filter(
        status__in=[Cliente.Status.ATIVO, Cliente.Status.INADIMPLENTE],
        cnh_validade__lte=limite,
    ):
        alertas.append(
            _alerta(
                cliente.cnh_validade,
                hoje,
                f"CNH de {cliente.nome}",
                reverse("pessoas:editar", args=[cliente.pk]),
            )
        )
    return sorted(alertas, key=lambda a: a["data"])


def ipva_em_aberto(limite):
    """Parcelas de IPVA sem pagamento vencendo até `limite` (carros não vendidos)."""
    return PagamentoIpva.objects.filter(vencimento__lte=limite, pago_em__isnull=True).exclude(
        veiculo__status=Veiculo.Status.VENDIDO
    )


def sem_ipva_do_ano(ano, veiculos=None):
    """Carros não vendidos com IPVA de anos anteriores e nenhuma linha do `ano`.

    É o aviso da virada do ano: sem ele, o IPVA novo podia nunca ser lançado e
    nenhum alerta aparecia (o do ano anterior já estava pago).
    """
    veiculos = veiculos if veiculos is not None else Veiculo.objects.all()
    return (
        veiculos.exclude(status=Veiculo.Status.VENDIDO)
        .filter(pagamentos_ipva__ano__lt=ano)
        .exclude(pagamentos_ipva__ano=ano)
        .distinct()
        .order_by("placa")
    )


def _dias_texto(data, hoje):
    """'venceu em 01/10/2026' / 'vence hoje' / 'vence em 12 dias (05/11/2026)'."""
    dias = (data - hoje).days
    if dias < 0:
        return f"venceu em {data:%d/%m/%Y}"
    if dias == 0:
        return "vence hoje"
    return f"vence em {dias} dia{'s' if dias > 1 else ''} ({data:%d/%m/%Y})"


def pendencias_do_veiculo(veiculo, hoje=None, na_oficina=_BUSCAR):
    """O que precisa ser feito com um carro, para a faixa no topo do detalhe (proposta #16).

    Cada pendência: {"texto", "detalhe", "url", "acao", "grave"} — `url`/`acao`
    levam direto à tela que resolve. Lista vazia = tudo em dia. Carro vendido
    não tem pendências.
    """
    # imports locais: estes apps importam apps.frota.models
    from apps.km.models import pendencias_de_leitura
    from apps.manutencao.services import (
        STATUS_FORA_DO_ACOMPANHAMENTO,
        oficina_por_veiculo,
        resumo_preventivas,
    )
    from apps.multas.models import Multa
    from apps.sinistros.models import Sinistro

    hoje = hoje or timezone.localdate()
    if veiculo.status == Veiculo.Status.VENDIDO:
        return []
    limite = hoje + timedelta(days=DIAS_ALERTA_VIGENCIA)
    acompanhado = (
        veiculo.uso == Veiculo.Uso.LOCACAO and veiculo.status not in STATUS_FORA_DO_ACOMPANHAMENTO
    )
    pendencias = []

    def adicionar(texto, detalhe, url, acao, grave):
        pendencias.append(
            {"texto": texto, "detalhe": detalhe, "url": url, "acao": acao, "grave": grave}
        )

    if acompanhado:
        url_manutencao = reverse("manutencao:registrar", args=[veiculo.pk])
        url_detalhe = reverse("frota:detalhe", args=[veiculo.pk])

        def link_preventiva(prev):
            # mesmo link do quadro de preventivas: já vem com o item escolhido e,
            # se nunca houve troca registrada, abre "Informar última troca" (data e
            # KM em branco) em vez de "feita hoje" — e volta para a ficha do carro
            url = f"{url_manutencao}?item={prev.item.pk}"
            if prev.sem_registro:
                url += "&ultima=1"
            url += f"&volta={quote(url_detalhe)}"
            acao = "Informar última troca" if prev.sem_registro else "Registrar que foi feita"
            return url, acao

        for prev in resumo_preventivas(veiculo):
            if not prev.em_alerta:
                continue
            url_prev, acao_prev = link_preventiva(prev)
            if prev.status == prev.VENCIDA:
                detalhe = f"passou {milhar(-prev.faltam_km)} km do ponto da troca"
                if prev.faltam_km == 0:
                    detalhe = "chegou no KM da troca"
                adicionar(
                    f"{prev.item.nome}: vencida",
                    detalhe,
                    url_prev,
                    acao_prev,
                    True,
                )
            else:
                detalhe = f"faltam {milhar(prev.faltam_km)} km"
                dias = prev.dias_restantes
                if dias is not None:
                    detalhe += (
                        " (deve chegar nos próximos dias)"
                        if dias <= 1
                        else f" (uns {dias} dias no ritmo atual)"
                    )
                adicionar(
                    f"{prev.item.nome}: está chegando a hora",
                    detalhe,
                    url_prev,
                    acao_prev,
                    False,
                )

        # mesma regra do painel e da tela de KM (D21): também os meses anteriores
        # esquecidos, não só o mês atual
        for leitura in pendencias_de_leitura(hoje):
            if leitura.veiculo.pk != veiculo.pk:
                continue
            mes = leitura.mes
            nome_mes = date_format(mes, "F").lower()
            if mes.year != hoje.year:
                nome_mes += f" de {mes.year}"
            adicionar(
                f"KM de {nome_mes} ainda não anotado",
                "esse mês já passou e a leitura ficou faltando"
                if leitura.atrasada
                else "anote quanto marca o painel do carro",
                f"{reverse('km:lista')}?mes={mes:%Y-%m}",
                "Anotar o KM",
                leitura.atrasada,
            )

    url_editar = reverse("frota:veiculo_editar", args=[veiculo.pk])
    url_ipva = reverse("frota:ipva", args=[veiculo.pk])
    # uma consulta só (ou nenhuma, se a tela já fez prefetch de pagamentos_ipva)
    ipvas = list(veiculo.pagamentos_ipva.all())
    em_aberto = sorted(
        (p for p in ipvas if p.vencimento and not p.pago_em and p.vencimento <= limite),
        key=lambda p: p.vencimento,
    )
    for parcela in em_aberto:
        adicionar(
            f"{parcela.rotulo} em aberto",
            _dias_texto(parcela.vencimento, hoje)
            + (f" — {brl(parcela.valor)}" if parcela.valor else ""),
            url_ipva,
            "Marcar como pago",
            parcela.vencimento < hoje,
        )
    anos = {p.ano for p in ipvas}
    if anos and min(anos) < hoje.year and hoje.year not in anos:  # mesma regra de sem_ipva_do_ano
        adicionar(
            f"IPVA {hoje.year} ainda não cadastrado",
            "lance o valor e o vencimento do IPVA deste ano",
            url_ipva,
            "Lançar o IPVA",
            False,
        )
    if veiculo.licenciamento_vencimento and veiculo.licenciamento_vencimento <= limite:
        adicionar(
            "Licenciamento",
            _dias_texto(veiculo.licenciamento_vencimento, hoje),
            url_editar,
            "Atualizar a data",
            veiculo.licenciamento_vencimento < hoje,
        )
    # mesmas vigências do painel (vigencias_a_vencer), agora também na página do carro
    if veiculo.rastreador_vigencia_fim and veiculo.rastreador_vigencia_fim <= limite:
        adicionar(
            "Rastreador"
            + (f" ({veiculo.rastreador_fornecedor})" if veiculo.rastreador_fornecedor else ""),
            _dias_texto(veiculo.rastreador_vigencia_fim, hoje),
            url_editar,
            "Atualizar a data",
            veiculo.rastreador_vigencia_fim < hoje,
        )
    if veiculo.bateria_garantia_fim and veiculo.bateria_garantia_fim <= limite:
        adicionar(
            "Garantia da bateria",
            _dias_texto(veiculo.bateria_garantia_fim, hoje),
            url_editar,
            "Atualizar a data",
            veiculo.bateria_garantia_fim < hoje,
        )

    if na_oficina is _BUSCAR:
        na_oficina = oficina_por_veiculo([veiculo.pk]).get(veiculo.pk)
    if na_oficina:
        dias = na_oficina.dias_parado
        adicionar(
            f"Na oficina desde {na_oficina.data_entrada:%d/%m/%Y}"
            + (f" ({na_oficina.oficina})" if na_oficina.oficina else ""),
            f"{dias} dia{'s' if dias != 1 else ''} parado — quando o carro voltar, registre aqui",
            f"{reverse('manutencao:voltou', args=[na_oficina.pk])}"
            f"?volta={quote(reverse('frota:detalhe', args=[veiculo.pk]))}",
            "Carro voltou da oficina",
            False,
        )

    # mesma regra do atalho "Indicar condutor" da lista de multas: multa anulada
    # (cancelada/recurso deferido) não precisa mais de FICI
    multas_sem_motorista = (
        Multa.objects.fici_pendente()
        .filter(veiculo=veiculo)
        .aggregate(total=Count("id"), prazo=Min("fici_prazo"))
    )
    total_multas, prazo = multas_sem_motorista["total"], multas_sem_motorista["prazo"]
    if total_multas:
        adicionar(
            f"{total_multas} multa{'s' if total_multas > 1 else ''} sem indicar o motorista (FICI)",
            f"prazo mais curto: {prazo:%d/%m/%Y}" if prazo else "indique quem dirigia",
            f"{reverse('multas:lista')}?veiculo={veiculo.placa}&tarefa=indicar",
            "Indicar o motorista",
            bool(prazo and prazo <= hoje + timedelta(days=7)),
        )

    sinistros_abertos = veiculo.sinistros.exclude(status=Sinistro.Status.CONCLUIDO).count()
    if sinistros_abertos:
        adicionar(
            f"{sinistros_abertos} sinistro{'s' if sinistros_abertos > 1 else ''} em aberto",
            "batida, roubo ou outro problema ainda não resolvido",
            f"{reverse('sinistros:lista')}?veiculo={veiculo.placa}",
            "Ver o sinistro",
            False,
        )
    # as graves (vencidas) primeiro, mantendo a ordem acima entre as demais
    return sorted(pendencias, key=lambda p: not p["grave"])
