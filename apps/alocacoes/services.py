"""Quem estava com o carro em cada data + linha do tempo do veículo (docs.md §4.2)."""

from django.db.models import Q

from .models import Alocacao, TrocaTemporaria


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


def linha_do_tempo(veiculo):
    """Histórico cronológico do veículo — substitui o diário em texto das planilhas."""
    eventos = []
    for alocacao in veiculo.alocacoes.select_related("cliente"):
        eventos.append(
            {
                "data": alocacao.data_inicio,
                "tipo": "Alocação",
                "descricao": f"Entregue a {alocacao.cliente.nome} "
                f"(R$ {alocacao.valor_semanal}/semana, {alocacao.km_entrega} km)",
            }
        )
        if alocacao.data_termino:
            eventos.append(
                {
                    "data": alocacao.data_termino,
                    "tipo": "Devolução",
                    "descricao": f"Devolvido por {alocacao.cliente.nome}"
                    + (f" ({alocacao.km_devolucao} km)" if alocacao.km_devolucao else ""),
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
                "descricao": f"Emprestado a {cliente} como substituto"
                + (f" — {troca.motivo}" if troca.motivo else ""),
            }
        )
        if troca.data_devolucao:
            eventos.append(
                {
                    "data": troca.data_devolucao,
                    "tipo": "Fim da troca",
                    "descricao": f"Devolvido por {cliente} ({troca.km_devolucao} km)",
                }
            )
    for troca in TrocaTemporaria.objects.filter(alocacao__veiculo=veiculo).select_related(
        "alocacao__cliente", "veiculo_substituto"
    ):
        eventos.append(
            {
                "data": troca.data_retirada,
                "tipo": "Carro na oficina",
                "descricao": f"{troca.alocacao.cliente.nome} ficou com o substituto "
                f"{troca.veiculo_substituto.placa}"
                + (f" — {troca.motivo}" if troca.motivo else ""),
            }
        )
    for manutencao in veiculo.manutencoes.select_related("item"):
        rotulo = manutencao.item.nome if manutencao.item else manutencao.get_tipo_display()
        eventos.append(
            {
                "data": manutencao.data,
                "tipo": "Manutenção",
                "descricao": rotulo + (f" aos {manutencao.km} km" if manutencao.km else ""),
            }
        )
    return sorted(eventos, key=lambda e: e["data"], reverse=True)
