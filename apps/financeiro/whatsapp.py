"""Mensagem de cobrança pronta para o WhatsApp (roadmap §7, versão que cabe no negócio).

Gera o link wa.me com o texto preenchido — a Luciana só confere e envia.
Nada é enviado automaticamente: o link abre o WhatsApp dela com a mensagem.
"""

from urllib.parse import quote

from django.conf import settings


def telefone_whatsapp(telefone):
    """Só dígitos, com DDI 55 — mesmo critério do hub de clientes."""
    digitos = "".join(c for c in (telefone or "") if c.isdigit())
    if not digitos:
        return ""
    if not digitos.startswith("55") or len(digitos) < 12:
        digitos = "55" + digitos
    return digitos


def mensagem_cobranca(cobranca, hoje):
    """Texto da cobrança — variação para em dia × atrasada."""
    nome = cobranca.cliente.nome.split()[0]
    valor = f"R$ {cobranca.saldo:.2f}".replace(".", ",")
    vencimento = cobranca.vencimento.strftime("%d/%m")
    if cobranca.vencimento < hoje:
        dias = (hoje - cobranca.vencimento).days
        corpo = (
            f"Olá, {nome}! Passando para lembrar do aluguel que venceu em {vencimento} "
            f"({dias} dia{'s' if dias != 1 else ''} atrás): {valor}."
        )
    else:
        corpo = f"Olá, {nome}! O aluguel da semana vence em {vencimento}: {valor}."
    if settings.CHAVE_PIX:
        corpo += f" Pix: {settings.CHAVE_PIX}"
    corpo += " Qualquer coisa é só chamar! — Trade Rent a Car"
    return corpo


def link_cobranca(cobranca, hoje):
    """URL wa.me com a mensagem pronta, ou '' se o cliente não tem telefone."""
    numero = telefone_whatsapp(cobranca.cliente.telefone)
    if not numero:
        return ""
    return f"https://wa.me/{numero}?text={quote(mensagem_cobranca(cobranca, hoje))}"
