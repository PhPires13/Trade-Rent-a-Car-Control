"""Mensagem de cobrança pronta para o WhatsApp (roadmap §7, versão que cabe no negócio).

Gera o link wa.me com o texto preenchido — a Luciana só confere e envia.
Nada é enviado automaticamente: o link abre o WhatsApp dela com a mensagem
(e o próprio WhatsApp deixa editar antes de mandar, por isso não há prévia).

O texto acompanha a origem da cobrança — uma ND de multas não pode chegar
ao motorista como "aluguel". Cobrança em judicial não tem botão (decisão
dos donos, proposta #3 opção A): ali a conversa é pelo advogado.
"""

from urllib.parse import quote

from django.conf import settings

from apps.formatos import brl

from .models import ZERO, Cobranca

ASSINATURA = "Qualquer coisa é só chamar! — Trade Rent a Car"

#: Como cada origem aparece na frase ("lembrar que <isto> venceu…") e o
#: começo da descrição que já repete a origem (some para não duplicar).
_ROTULOS = {
    Cobranca.Origem.ENCARGO: ("o encargo por atraso", "encargo por atraso"),
    Cobranca.Origem.EXCEDENTE_KM: ("o excedente de km", "excedente de km"),
    Cobranca.Origem.REPASSE_MANUTENCAO: ("o repasse de manutenção", "repasse manutenção"),
    Cobranca.Origem.REPASSE_SINISTRO: ("o repasse de sinistro", "repasse de sinistro"),
}


#: Aviso do cadastro quando o telefone não dá para usar no WhatsApp.
MENSAGEM_TELEFONE_SEM_DDD = "Informe o telefone com DDD, ex.: (31) 98888-7777"


def telefone_whatsapp(telefone):
    """Só dígitos, com DDI 55 — ou '' se o número não tem DDD.

    Aceita 10/11 dígitos (DDD + fixo/celular) ou 12/13 começando com 55 (já
    com DDI). Sem DDD o wa.me abriria uma conversa com número inexistente;
    vazio faz a tela mostrar "telefone incompleto" e levar ao cadastro.
    """
    digitos = "".join(c for c in (telefone or "") if c.isdigit())
    if len(digitos) in (10, 11):
        return "55" + digitos
    if len(digitos) in (12, 13) and digitos.startswith("55"):
        return digitos
    return ""


def _primeiro_nome(cliente):
    """'Arlen' de 'Arlen Souza' — nome em branco não derruba a tela."""
    partes = (cliente.nome or "").split()
    return partes[0] if partes else "tudo bem"


def _minuscula(texto):
    return texto[:1].lower() + texto[1:]


def _maiuscula(texto):
    return texto[:1].upper() + texto[1:]


def _aluguel(cobranca):
    """'o aluguel da semana' × 'o aluguel de 3 dias (13/10 a 15/10)' (semana proporcional)."""
    inicio, fim = cobranca.periodo_inicio, cobranca.periodo_fim
    if inicio is None or fim is None or (fim - inicio).days == 6:
        return "o aluguel da semana"
    dias = (fim - inicio).days + 1
    if dias == 1:
        return f"o aluguel de 1 dia ({inicio:%d/%m})"
    return f"o aluguel de {dias} dias ({inicio:%d/%m} a {fim:%d/%m})"


def o_que_e(cobranca):
    """A cobrança em palavras, com artigo: 'a nota de débito ND 046 (multas)'."""
    descricao = cobranca.descricao.strip()
    if cobranca.origem == Cobranca.Origem.ALUGUEL:
        placa = cobranca.alocacao.veiculo.placa if cobranca.alocacao_id else ""
        return _aluguel(cobranca) + (f" do carro {placa}" if placa else "")
    if cobranca.origem == Cobranca.Origem.NOTA_DEBITO:
        return "a " + _minuscula(descricao)
    if cobranca.origem in _ROTULOS:
        rotulo, prefixo = _ROTULOS[cobranca.origem]
        detalhe = descricao
        if detalhe.lower().startswith(prefixo):
            detalhe = detalhe[len(prefixo) :].strip(" —-")
        return rotulo + (f" ({detalhe})" if detalhe else "")
    return f"a cobrança “{descricao}”"


def _quando(cobranca, hoje):
    """'venceu em 14/10 (6 dias atrás)' × 'vence hoje (20/10)' × 'vence em 22/10'."""
    vencimento = cobranca.vencimento.strftime("%d/%m")
    if cobranca.vencimento < hoje:
        dias = (hoje - cobranca.vencimento).days
        return f"venceu em {vencimento} ({dias} dia{'s' if dias != 1 else ''} atrás)"
    if cobranca.vencimento == hoje:
        return f"vence hoje ({vencimento})"
    return f"vence em {vencimento}"


def _fechamento():
    # chave Pix sozinha no fim da linha: sem ponto colado, para copiar sem erro
    if settings.CHAVE_PIX:
        return f" Pix: {settings.CHAVE_PIX}\n{ASSINATURA}"
    return f" {ASSINATURA}"


def mensagem_cobranca(cobranca, hoje):
    """Texto de uma cobrança — o que é (pela origem), quando vence e quanto falta."""
    nome = _primeiro_nome(cobranca.cliente)
    valor = brl(cobranca.saldo)
    quando = _quando(cobranca, hoje)
    if cobranca.vencimento < hoje:
        corpo = f"Olá, {nome}! Passando para lembrar que {o_que_e(cobranca)} {quando}: {valor}."
    else:
        corpo = f"Olá, {nome}! {_maiuscula(o_que_e(cobranca))} {quando}: {valor}."
    return corpo + _fechamento()


def _cobravel(cobranca):
    """Só o que está no fluxo normal e ainda tem saldo — judicial fica de fora."""
    return cobranca.status in Cobranca.STATUS_EM_ABERTO and cobranca.saldo > ZERO


def _link(cliente, texto):
    numero = telefone_whatsapp(cliente.telefone)
    if not numero or not texto:
        return ""
    return f"https://wa.me/{numero}?text={quote(texto)}"


def link_cobranca(cobranca, hoje):
    """URL wa.me com a mensagem pronta, ou '' (sem telefone, judicial ou quitada)."""
    if not _cobravel(cobranca):
        return ""
    return _link(cobranca.cliente, mensagem_cobranca(cobranca, hoje))


def mensagem_cliente(cliente, cobrancas, hoje):
    """Uma mensagem só com tudo que o cliente tem em aberto e o total.

    `cobrancas` pode vir com judicial/paga misturadas: só entra o cobrável.
    Com um item só, vira a mensagem simples daquela cobrança.
    """
    itens = sorted((c for c in cobrancas if _cobravel(c)), key=lambda c: (c.vencimento, c.pk))
    if not itens:
        return ""
    if len(itens) == 1:
        return mensagem_cobranca(itens[0], hoje)
    nome = _primeiro_nome(cliente)
    linhas = [f"Olá, {nome}! Segue o que está em aberto com a Trade Rent a Car:"]
    linhas += [f"• {_maiuscula(o_que_e(c))}: {brl(c.saldo)} — {_quando(c, hoje)}" for c in itens]
    total = sum((c.saldo for c in itens), ZERO)
    linhas.append(f"Total: {brl(total)}.{_fechamento()}")
    return "\n".join(linhas)


def link_cliente(cliente, cobrancas, hoje):
    """URL wa.me com a mensagem única do cliente, ou '' se não há o que cobrar."""
    return _link(cliente, mensagem_cliente(cliente, cobrancas, hoje))


def mensagem_nd(nd, hoje):
    """A nota de débito para o motorista: itens, total, vencimento e Pix."""
    cobranca = getattr(nd, "cobranca", None)
    nome = _primeiro_nome(nd.cliente)
    itens = list(nd.itens.all())
    linhas = [f"Olá, {nome}! Segue a nota de débito ND {nd.numero:03d} da Trade Rent a Car:"]
    linhas += [f"• {item.descricao}: {brl(item.valor)}" for item in itens]
    total = f"Total: {brl(sum((item.valor for item in itens), ZERO))}"
    if cobranca is not None:
        if cobranca.status == Cobranca.Status.PARCIAL or (
            cobranca.status == Cobranca.Status.ATRASADO and cobranca.saldo < cobranca.valor
        ):
            total += f" (já recebemos {brl(cobranca.total_quitado)}; falta {brl(cobranca.saldo)})"
        total += f" — {_quando(cobranca, hoje)}"
    linhas.append(f"{total}.{_fechamento()}")
    return "\n".join(linhas)


def link_nd(nd, hoje):
    """Botão "Enviar ao motorista": só para ND com saldo no fluxo normal."""
    cobranca = getattr(nd, "cobranca", None)
    if cobranca is None or not _cobravel(cobranca):
        return ""
    return _link(nd.cliente, mensagem_nd(nd, hoje))
