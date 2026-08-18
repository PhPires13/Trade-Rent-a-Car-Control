"""Leitura automática da CNH no cadastro de motorista (base: apps/documentos.py)."""

from apps.documentos import (  # noqa: F401 — reexporta p/ views e testes
    TAMANHO_MAXIMO,
    TIPOS_ACEITOS,
    TIPOS_DE_IMAGEM,
    disponivel,
    extrair_estruturado,
    validar_upload,
)

ESQUEMA = {
    "type": "object",
    "properties": {
        "nome": {"type": ["string", "null"], "description": "Nome completo do condutor"},
        "cpf": {
            "type": ["string", "null"],
            "description": "CPF no formato 000.000.000-00",
        },
        "cnh_numero": {
            "type": ["string", "null"],
            "description": "Número de registro da CNH (campo Nº REGISTRO)",
        },
        "cnh_categoria": {
            "type": ["string", "null"],
            "description": "Categoria de habilitação (ex.: B, AB)",
        },
        "cnh_validade": {
            "type": ["string", "null"],
            "description": "Data de validade no formato AAAA-MM-DD",
        },
        "legivel": {
            "type": "boolean",
            "description": "false se os arquivos não permitem ler os dados com segurança",
        },
    },
    "required": ["nome", "cpf", "cnh_numero", "cnh_categoria", "cnh_validade", "legivel"],
    "additionalProperties": False,
}

INSTRUCAO = (
    "Os arquivos são a frente e/ou o verso de uma CNH brasileira (Carteira Nacional "
    "de Habilitação), enviados pelo próprio cadastro de um cliente de uma locadora. "
    "Extraia exatamente o que está impresso no documento, sem inventar: nome completo, "
    "CPF, número de registro da CNH, categoria e data de validade. Campo ilegível ou "
    "ausente = null. Se o arquivo não for uma CNH ou estiver ilegível, marque "
    "legivel=false e deixe os campos null."
)


def extrair_dados(fotos):
    """Lê as fotos/PDF da CNH e devolve o dict do ESQUEMA, ou None."""
    return extrair_estruturado(fotos, ESQUEMA, INSTRUCAO, rotulo="CNH")
