"""Leitura da foto do checklist de vistoria preenchido (base: apps/documentos.py).

O sistema imprime o checklist em branco, o preenchimento é à mão na entrega ou
devolução, e a foto do papel preenchido é lida para carregar os dados no
formulário de vistoria — validação humana antes de salvar.
"""

from apps.documentos import (  # noqa: F401 — reexporta p/ views e testes
    disponivel,
    extrair_estruturado,
    validar_upload,
)

ESQUEMA = {
    "type": "object",
    "properties": {
        "tipo": {
            "type": ["string", "null"],
            "enum": ["entrada", "saida", None],
            "description": "entrada (entrega ao cliente) ou saida (devolução), se marcado",
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
                "Resumo das marcas/avarias assinaladas ou escritas (item e estado), uma por linha"
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

INSTRUCAO = (
    "O arquivo é a foto de um checklist de vistoria de veículo preenchido à mão "
    "(entrada/saída de uma locadora). Transcreva exatamente o que está marcado ou "
    "escrito, sem inventar: tipo (entrada/saída), data, km do painel, nível de "
    "combustível e as marcas/avarias assinaladas (item e estado, uma por linha), além "
    "das notas. Campo em branco ou ilegível = null. Se a foto não for de um checklist "
    "ou estiver ilegível, marque legivel=false e deixe os campos null."
)


def extrair_dados(arquivos):
    """Lê a foto/PDF do checklist preenchido e devolve o dict do ESQUEMA, ou None."""
    return extrair_estruturado(arquivos, ESQUEMA, INSTRUCAO, rotulo="checklist de vistoria")
