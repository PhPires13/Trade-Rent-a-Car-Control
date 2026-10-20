"""Recibo de pagamento pronto para o WhatsApp (proposta de UX nº 1).

Mesmo esquema da cobrança: gera o link wa.me com o texto preenchido e a
Luciana confere e envia pelo WhatsApp dela — nada sai automaticamente.
"""

from urllib.parse import quote

from apps.formatos import brl

from .models import Recebimento
from .whatsapp import telefone_whatsapp


def mensagem_recibo(resumo):
    """Texto do recibo a partir de services.resumo_recebimento()."""
    recebimento = resumo["recebimento"]
    nome = recebimento.cliente.nome.split()[0] if recebimento.cliente.nome.strip() else ""
    if recebimento.forma == Recebimento.Forma.CREDITO:
        # não entrou dinheiro novo: "recebemos seu pagamento (Crédito)" confundiria o motorista
        abertura = (
            f"Olá, {nome}! Usamos {brl(recebimento.valor)} do seu crédito guardado "
            f"em {recebimento.data:%d/%m/%Y}."
        )
    else:
        abertura = (
            f"Olá, {nome}! Recebemos seu pagamento de {brl(recebimento.valor)} "
            f"({recebimento.get_forma_display()}) em {recebimento.data:%d/%m/%Y}."
        )
    linhas = [abertura]
    for cobranca, valor in resumo["quitadas"]:
        linhas.append(f"✓ Quitado: {cobranca.descricao} ({brl(valor)})")
    for cobranca, valor in resumo["parciais"]:
        linhas.append(
            f"• Abatido {brl(valor)} de {cobranca.descricao} (ainda falta {brl(cobranca.saldo)})"
        )
    if resumo["sobra"]:
        valor, destino = resumo["sobra"]
        if destino == "caucao":
            linhas.append(f"• {brl(valor)} foram para a sua caução.")
        else:
            linhas.append(
                f"• {brl(valor)} ficaram guardados como crédito para as próximas semanas."
            )
    if resumo["devido"] > 0:
        linhas.append(f"Ainda em aberto: {brl(resumo['devido'])}.")
    else:
        linhas.append("Você está em dia com a gente. Obrigado!")
    linhas.append("— Trade Rent a Car")
    return "\n".join(linhas)


def link_recibo(resumo):
    """URL wa.me com o recibo, ou '' se o cliente não tem telefone."""
    numero = telefone_whatsapp(resumo["recebimento"].cliente.telefone)
    if not numero:
        return ""
    return f"https://wa.me/{numero}?text={quote(mensagem_recibo(resumo))}"
