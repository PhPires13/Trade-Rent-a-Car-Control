"""Cartão "Próximos passos da entrega" — usado na ficha do cliente."""

from django import template

from apps.alocacoes.entrega import passos_da_entrega

register = template.Library()


@register.inclusion_tag("alocacoes/nova_passos_entrega.html", takes_context=True)
def passos_entrega(context, alocacao):
    passos = passos_da_entrega(alocacao)
    return {
        "alocacao": alocacao,
        "passos": passos,
        "faltam": sum(1 for passo in passos if not passo["feito"]) if passos else 0,
        "csrf_token": context.get("csrf_token"),
    }
