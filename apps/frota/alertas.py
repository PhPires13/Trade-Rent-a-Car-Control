"""Vigências e documentos a vencer — IPVA, licenciamento, rastreador, bateria e CNH (§5)."""

from datetime import date, timedelta

from django.db.models import Count, Min
from django.urls import reverse
from django.utils.formats import date_format

from apps.formatos import brl, milhar
from apps.pessoas.models import Cliente

from .models import Veiculo

DIAS_ALERTA_VIGENCIA = 30


def _alerta(data, hoje, descricao):
    return {"data": data, "vencido": data < hoje, "descricao": descricao}


def vigencias_a_vencer(hoje=None):
    """Alertas vencidos ou vencendo nos próximos 30 dias, do mais antigo ao mais novo."""
    hoje = hoje or date.today()
    limite = hoje + timedelta(days=DIAS_ALERTA_VIGENCIA)
    alertas = []
    frota = Veiculo.objects.exclude(status=Veiculo.Status.VENDIDO)
    # IPVA em aberto (sem data de pagamento) vencendo — decisão nº 21 / tabela do §5
    for veiculo in frota.filter(ipva_vencimento__lte=limite, ipva_pago_em__isnull=True):
        alertas.append(
            _alerta(
                veiculo.ipva_vencimento,
                hoje,
                f"IPVA do {veiculo.placa}"
                + (f" ({veiculo.ipva_ano})" if veiculo.ipva_ano else "")
                + (f" — {brl(veiculo.ipva_valor)}" if veiculo.ipva_valor else ""),
            )
        )
    for veiculo in frota.filter(licenciamento_vencimento__lte=limite):
        alertas.append(
            _alerta(veiculo.licenciamento_vencimento, hoje, f"Licenciamento do {veiculo.placa}")
        )
    for veiculo in frota.filter(rastreador_vigencia_fim__lte=limite):
        alertas.append(
            _alerta(
                veiculo.rastreador_vigencia_fim,
                hoje,
                f"Rastreador do {veiculo.placa}"
                + (f" ({veiculo.rastreador_fornecedor})" if veiculo.rastreador_fornecedor else ""),
            )
        )
    for veiculo in frota.filter(bateria_garantia_fim__lte=limite):
        alertas.append(
            _alerta(veiculo.bateria_garantia_fim, hoje, f"Garantia da bateria do {veiculo.placa}")
        )
    for cliente in Cliente.objects.filter(
        status__in=[Cliente.Status.ATIVO, Cliente.Status.INADIMPLENTE],
        cnh_validade__lte=limite,
    ):
        alertas.append(_alerta(cliente.cnh_validade, hoje, f"CNH de {cliente.nome}"))
    return sorted(alertas, key=lambda a: a["data"])


def _dias_texto(data, hoje):
    """'venceu em 01/10/2026' / 'vence hoje' / 'vence em 12 dias (05/11/2026)'."""
    dias = (data - hoje).days
    if dias < 0:
        return f"venceu em {data:%d/%m/%Y}"
    if dias == 0:
        return "vence hoje"
    return f"vence em {dias} dia{'s' if dias > 1 else ''} ({data:%d/%m/%Y})"


def pendencias_do_veiculo(veiculo, hoje=None):
    """O que precisa ser feito com um carro, para a faixa no topo do detalhe (proposta #16).

    Cada pendência: {"texto", "detalhe", "url", "acao", "grave"} — `url`/`acao`
    levam direto à tela que resolve. Lista vazia = tudo em dia. Carro vendido
    não tem pendências.
    """
    # imports locais: estes apps importam apps.frota.models
    from apps.km.models import RegistroKm, primeiro_dia_do_mes
    from apps.manutencao.services import STATUS_FORA_DO_ACOMPANHAMENTO, resumo_preventivas
    from apps.multas.models import Multa
    from apps.sinistros.models import Sinistro

    hoje = hoje or date.today()
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
        for prev in resumo_preventivas(veiculo):
            if not prev.em_alerta:
                continue
            if prev.status == prev.VENCIDA:
                detalhe = f"passou {milhar(-prev.faltam_km)} km do ponto da troca"
                if prev.faltam_km == 0:
                    detalhe = "chegou no KM da troca"
                adicionar(
                    f"{prev.item.nome}: vencida",
                    detalhe,
                    url_manutencao,
                    "Registrar que foi feita",
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
                    url_manutencao,
                    "Registrar que foi feita",
                    False,
                )

        mes = primeiro_dia_do_mes(hoje)
        if not RegistroKm.objects.filter(veiculo=veiculo, mes_referencia=mes).exists():
            adicionar(
                f"KM de {date_format(mes, 'F').lower()} ainda não anotado",
                "anote quanto marca o painel do carro",
                f"{reverse('km:lista')}?mes={mes:%Y-%m}",
                "Anotar o KM",
                False,
            )

    url_editar = reverse("frota:veiculo_editar", args=[veiculo.pk])
    if veiculo.ipva_vencimento and not veiculo.ipva_pago_em and veiculo.ipva_vencimento <= limite:
        adicionar(
            "IPVA" + (f" {veiculo.ipva_ano}" if veiculo.ipva_ano else "") + " em aberto",
            _dias_texto(veiculo.ipva_vencimento, hoje)
            + (f" — {brl(veiculo.ipva_valor)}" if veiculo.ipva_valor else ""),
            url_editar,
            "Marcar como pago",
            veiculo.ipva_vencimento < hoje,
        )
    if veiculo.licenciamento_vencimento and veiculo.licenciamento_vencimento <= limite:
        adicionar(
            "Licenciamento",
            _dias_texto(veiculo.licenciamento_vencimento, hoje),
            url_editar,
            "Atualizar a data",
            veiculo.licenciamento_vencimento < hoje,
        )

    # mesma regra do atalho "Indicar condutor" da lista de multas: multa anulada
    # (cancelada/recurso deferido) não precisa mais de FICI
    multas_sem_motorista = (
        Multa.objects.filter(veiculo=veiculo, fici_status=Multa.Fici.PENDENTE)
        .exclude(resultado__in=Multa.RESULTADOS_ANULADORES)
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
