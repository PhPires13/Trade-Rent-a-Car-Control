"""Quem estava com o carro em cada data + linha do tempo do veículo (docs.md §4.2)."""

from django.db.models import Q

from apps.formatos import brl, milhar

from .models import Alocacao, TrocaTemporaria, Vistoria


def cliente_vigente(veiculo, data):
    """Cliente responsável pelo veículo na data, considerando trocas temporárias.

    Regra (docs.md §4.2 e §4.7): multas, sinistros e KM são atribuídos a quem
    estava com o carro no dia — inclusive quando o carro era um substituto.
    """
    troca = (
        TrocaTemporaria.objects.filter(veiculo_substituto=veiculo, data_retirada__lte=data)
        .filter(Q(data_devolucao__isnull=True) | Q(data_devolucao__gte=data))
        .select_related("alocacao__cliente")
        .order_by("-data_retirada")
        .first()
    )
    if troca:
        return troca.alocacao.cliente
    alocacao = (
        Alocacao.objects.filter(veiculo=veiculo, data_inicio__lte=data)
        .filter(Q(data_termino__isnull=True) | Q(data_termino__gte=data))
        .select_related("cliente")
        .order_by("-data_inicio")
        .first()
    )
    return alocacao.cliente if alocacao else None


#: Ordem dos eventos de um mesmo dia (do primeiro ao último a acontecer).
_ORDEM_NO_DIA = {
    "entrega": 0,
    "vistoria_entrega": 1,
    "oficina": 2,
    "troca": 3,
    "saida_oficina": 4,
    "fim_troca": 5,
    "vistoria_devolucao": 6,
    "devolucao": 7,
}


def linha_do_tempo(veiculo):
    """Histórico cronológico do veículo — substitui o diário em texto das planilhas."""
    eventos = []
    for alocacao in veiculo.alocacoes.select_related("cliente"):
        eventos.append(
            {
                "data": alocacao.data_inicio,
                "tipo": "Alocação",
                "ordem": _ORDEM_NO_DIA["entrega"],
                "descricao": f"Entregue a {alocacao.cliente.nome} "
                f"({brl(alocacao.valor_semanal)}/semana, {milhar(alocacao.km_entrega)} km)",
            }
        )
        if alocacao.data_termino:
            eventos.append(
                {
                    "data": alocacao.data_termino,
                    "tipo": "Devolução",
                    "ordem": _ORDEM_NO_DIA["devolucao"],
                    "descricao": f"Devolvido por {alocacao.cliente.nome}"
                    + (f" ({milhar(alocacao.km_devolucao)} km)" if alocacao.km_devolucao else ""),
                }
            )
    for troca in TrocaTemporaria.objects.filter(veiculo_substituto=veiculo).select_related(
        "alocacao__cliente"
    ):
        cliente = troca.alocacao.cliente.nome
        eventos.append(
            {
                "data": troca.data_retirada,
                "tipo": "Troca temporária",
                "ordem": _ORDEM_NO_DIA["troca"],
                "descricao": f"Emprestado a {cliente} como substituto"
                + (f" — {troca.motivo}" if troca.motivo else ""),
            }
        )
        if troca.data_devolucao:
            eventos.append(
                {
                    "data": troca.data_devolucao,
                    "tipo": "Fim da troca",
                    "ordem": _ORDEM_NO_DIA["fim_troca"],
                    "descricao": f"Devolvido por {cliente} ({milhar(troca.km_devolucao)} km)",
                }
            )
    for troca in TrocaTemporaria.objects.filter(alocacao__veiculo=veiculo).select_related(
        "alocacao__cliente", "veiculo_substituto"
    ):
        eventos.append(
            {
                "data": troca.data_retirada,
                "tipo": "Carro na oficina",
                "ordem": _ORDEM_NO_DIA["troca"],
                "descricao": f"{troca.alocacao.cliente.nome} ficou com o substituto "
                f"{troca.veiculo_substituto.placa}"
                + (f" — {troca.motivo}" if troca.motivo else ""),
            }
        )
        if troca.data_devolucao:
            eventos.append(
                {
                    "data": troca.data_devolucao,
                    "tipo": "Fim da troca",
                    "ordem": _ORDEM_NO_DIA["fim_troca"],
                    "descricao": f"Voltou para o carro dele: {troca.alocacao.cliente.nome} "
                    f"devolveu o substituto {troca.veiculo_substituto.placa}",
                }
            )
    # C27: a vistoria registrada aparece na linha do tempo para onde a tela leva
    vistorias = Vistoria.objects.filter(
        Q(veiculo=veiculo) | Q(veiculo__isnull=True, alocacao__veiculo=veiculo)
    ).select_related("alocacao__cliente")
    for vistoria in vistorias:
        detalhes = [vistoria.alocacao.cliente.nome]
        if vistoria.km:
            detalhes.append(f"{milhar(vistoria.km)} km")
        if vistoria.combustivel:
            detalhes.append(f"combustível {vistoria.get_combustivel_display()}")
        descricao = " · ".join(detalhes)
        if vistoria.avarias:
            descricao += f" — marcas: {vistoria.avarias}"
        eventos.append(
            {
                "data": vistoria.data,
                "tipo": f"Vistoria ({vistoria.get_tipo_display().lower()})",
                "ordem": _ORDEM_NO_DIA[
                    "vistoria_entrega"
                    if vistoria.tipo == Vistoria.Tipo.ENTRADA
                    else "vistoria_devolucao"
                ],
                "descricao": descricao,
                "url": vistoria.foto.url if vistoria.foto else "",
            }
        )
    for manutencao in veiculo.manutencoes.select_related("item", "oficina"):
        rotulo = manutencao.item.nome if manutencao.item else manutencao.get_tipo_display()
        partes = [rotulo + (f" aos {milhar(manutencao.km)} km" if manutencao.km else "")]
        if manutencao.descricao:
            partes.append(manutencao.descricao)
        if manutencao.oficina:
            partes.append(manutencao.oficina.nome)
        eventos.append(
            {
                "data": manutencao.data,
                "tipo": "Manutenção",
                "ordem": _ORDEM_NO_DIA["oficina"],
                "descricao": " · ".join(partes),
            }
        )
        if manutencao.data_saida:
            eventos.append(
                {
                    "data": manutencao.data_saida,
                    "tipo": "Saiu da oficina",
                    "ordem": _ORDEM_NO_DIA["saida_oficina"],
                    "descricao": rotulo
                    + (f" · {manutencao.oficina.nome}" if manutencao.oficina else ""),
                }
            )
    # mais recente no topo; no mesmo dia, na ordem em que as coisas acontecem
    return sorted(eventos, key=lambda e: (e["data"], e["ordem"]), reverse=True)


def refazer_semanas_da_troca(troca, hoje=None):
    """Troca que perdeu o valor ajustado (Admin): semanas em aberto voltam ao preço certo.

    Espelha financeiro.services.reajustar_aluguel_pela_troca (decisão D02), que
    só age quando há valor ajustado. Mexe só nas semanas ainda em aberto que a
    troca alcança, e nunca deixa o valor abaixo do que já foi pago. Devolve as
    cobranças alteradas.
    """
    from datetime import timedelta

    from apps.financeiro.models import Cobranca  # import local evita ciclo de import
    from apps.financeiro.services import (
        _valor_do_periodo,
        atualizar_inadimplencia,
        periodo_da_cobranca,
    )

    alocacao = troca.alocacao
    abertas = Cobranca.com_quitacao_anotada(
        Cobranca.objects.filter(
            alocacao=alocacao,
            origem=Cobranca.Origem.ALUGUEL,
            status__in=Cobranca.STATUS_EM_ABERTO,
            vencimento__gte=troca.data_retirada - timedelta(days=6),
        )
    )
    alteradas = []
    for cobranca in abertas:
        inicio, fim = periodo_da_cobranca(cobranca)
        if fim < troca.data_retirada:
            continue
        if troca.data_devolucao and inicio > troca.data_devolucao:
            continue
        novo = _valor_do_periodo(alocacao, inicio, fim)
        if novo == cobranca.valor or novo < cobranca.total_quitado:
            continue
        cobranca.valor = novo
        cobranca.save(update_fields=["valor"])
        cobranca.atualizar_status(hoje)
        alteradas.append(cobranca)
    if alteradas:
        atualizar_inadimplencia(alocacao.cliente)
    return alteradas
