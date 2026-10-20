"""Selo "Na oficina" para as telas da frota — derivado das manutenções em aberto."""

from django import template

from apps.manutencao.services import oficina_por_veiculo

register = template.Library()


@register.simple_tag
def carros_na_oficina(veiculos):
    """{veiculo_id: Manutencao em aberto} numa query só.

    Aceita veículos ou os cartões do hub (dicionários com "veiculo"); uso:
    {% carros_na_oficina cards as na_oficina %}
    """
    ids = [(v["veiculo"] if isinstance(v, dict) else v).pk for v in veiculos]
    return oficina_por_veiculo(ids)


@register.simple_tag
def manutencao_na_oficina(veiculo):
    """Manutenção em aberto do carro (ou None) — para a página de um carro só."""
    return oficina_por_veiculo([veiculo.pk]).get(veiculo.pk)


@register.filter
def pegar(dicionario, chave):
    """{{ na_oficina|pegar:veiculo.pk }} — dicionário por chave no template."""
    return dicionario.get(chave)
