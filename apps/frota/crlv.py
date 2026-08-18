"""Leitura automática do documento do carro (CRLV) no cadastro de veículo.

Foto ou PDF do CRLV/CRLV-e → placa, renavam, chassi, marca/modelo e ano
preenchidos no formulário como sugestão (base: apps/documentos.py).
"""

from apps.documentos import (  # noqa: F401 — reexporta p/ views e testes
    disponivel,
    extrair_estruturado,
    validar_upload,
)

ESQUEMA = {
    "type": "object",
    "properties": {
        "placa": {"type": ["string", "null"], "description": "Placa do veículo (ex.: ABC1D23)"},
        "renavam": {"type": ["string", "null"], "description": "Código RENAVAM"},
        "chassi": {"type": ["string", "null"], "description": "Número do chassi (17 caracteres)"},
        "marca_modelo": {
            "type": ["string", "null"],
            "description": "Marca/modelo/versão como impresso (ex.: VW/GOL 1.0)",
        },
        "ano": {
            "type": ["string", "null"],
            "description": "Ano de fabricação/modelo no formato AAAA/AAAA (ex.: 2020/2021)",
        },
        "legivel": {
            "type": "boolean",
            "description": "false se os arquivos não permitem ler os dados com segurança",
        },
    },
    "required": ["placa", "renavam", "chassi", "marca_modelo", "ano", "legivel"],
    "additionalProperties": False,
}

INSTRUCAO = (
    "O arquivo é um CRLV/CRLV-e brasileiro (Certificado de Registro e Licenciamento de "
    "Veículo), enviado pelo cadastro da frota de uma locadora. Extraia exatamente o que "
    "está impresso, sem inventar: placa, código RENAVAM, chassi, marca/modelo/versão e "
    "ano de fabricação/modelo. Campo ilegível ou ausente = null. Se o arquivo não for "
    "um CRLV ou estiver ilegível, marque legivel=false e deixe os campos null."
)


def extrair_dados(arquivos):
    """Lê a foto/PDF do CRLV e devolve o dict do ESQUEMA, ou None."""
    return extrair_estruturado(arquivos, ESQUEMA, INSTRUCAO, rotulo="CRLV")
