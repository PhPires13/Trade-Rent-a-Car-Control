"""Checklist de vistoria: itens impressos e leitura da foto preenchida.

O sistema imprime o checklist em branco (ITENS_CHECKLIST), o preenchimento é à
mão na entrega ou devolução, e a foto do papel preenchido é lida para carregar
os dados no formulário de vistoria — validação humana antes de salvar (base:
apps/documentos.py).
"""

from apps.documentos import (  # noqa: F401 — reexporta p/ views
    disponivel,
    extrair_estruturado,
    validar_upload,
)

#: Itens impressos no checklist em branco — uma linha por região do carro.
ITENS_CHECKLIST = [
    "Para-choque dianteiro",
    "Para-choque traseiro",
    "Capô",
    "Teto",
    "Porta-malas / tampa",
    "Lateral esquerda (portas e paralamas)",
    "Lateral direita (portas e paralamas)",
    "Retrovisores",
    "Vidros e para-brisa",
    "Faróis e lanternas",
    "Pneus e rodas (4 + estepe)",
    "Bancos e forros",
    "Painel e comandos",
    "Tapetes / interior",
    "Macaco, chave de roda e triângulo",
    "Documento do carro no porta-luvas",
]

ESQUEMA = {
    "type": "object",
    "properties": {
        "tipo": {
            "type": ["string", "null"],
            "enum": ["entrada", "saida", None],
            "description": (
                'Quadrinho marcado: "Entrega ao motorista" = entrada; '
                '"Devolução pelo motorista" = saida; nenhum marcado = null'
            ),
        },
        "data": {
            "type": ["string", "null"],
            "description": "Data escrita no formulário, formato AAAA-MM-DD",
        },
        "km": {
            "type": ["integer", "null"],
            "description": "Quilometragem do painel escrita no formulário",
        },
        "combustivel": {
            "type": ["string", "null"],
            "enum": ["cheio", "tres_quartos", "meio", "um_quarto", "reserva", None],
            "description": "Nível de combustível marcado",
        },
        "avarias": {
            "type": ["string", "null"],
            "description": (
                "Resumo das marcas/avarias assinaladas na tabela, escritas ou desenhadas no "
                "desenho do carro (onde e o quê), uma por linha"
            ),
        },
        "notas": {"type": ["string", "null"], "description": "Notas/observações escritas"},
        "legivel": {
            "type": "boolean",
            "description": "false se a foto não permite ler o formulário com segurança",
        },
    },
    "required": ["tipo", "data", "km", "combustivel", "avarias", "notas", "legivel"],
    "additionalProperties": False,
}

#: Legenda das letras para marcar avarias no desenho do carro (impressa no checklist).
LEGENDA_AVARIAS = [
    ("R", "risco"),
    ("A", "amassado"),
    ("T", "trinca ou quebrado"),
    ("F", "falta a peça"),
]

INSTRUCAO = (
    "O arquivo é a foto de um checklist de vistoria de veículo preenchido à mão "
    "numa locadora. Transcreva exatamente o que está marcado ou escrito, sem inventar. "
    'Tipo: o quadrinho "Entrega ao motorista" (o carro sai da locadora com o motorista) '
    'vira "entrada"; "Devolução pelo motorista" (o carro volta para a locadora) vira '
    '"saida" — não deduza o tipo por outras palavras do papel. Depois: data, km do '
    "painel, nível de combustível, as marcas/avarias assinaladas na tabela (item e "
    "estado) e as marcadas no desenho do carro, onde as letras querem dizer "
    + ", ".join(f"{letra} = {nome}" for letra, nome in LEGENDA_AVARIAS)
    + " (escreva a região do carro e o que é, uma por linha), além das notas. Campo em "
    "branco ou ilegível = null. Se a foto não for de um checklist ou estiver ilegível, "
    "marque legivel=false e deixe os campos null."
)


def extrair_dados(arquivos):
    """Lê a foto/PDF do checklist preenchido e devolve o dict do ESQUEMA, ou None."""
    return extrair_estruturado(arquivos, ESQUEMA, INSTRUCAO, rotulo="checklist de vistoria")
