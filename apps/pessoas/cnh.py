"""Leitura automática da CNH no cadastro de motorista.

As fotos (frente/verso) vão para a API do Claude com saída estruturada; os
dados extraídos voltam para o formulário como SUGESTÃO — quem cadastra valida
e corrige antes de salvar (nada é gravado sem confirmação humana).

Sem ANTHROPIC_API_KEY configurada, o recurso fica desligado e o cadastro
funciona normalmente (só sem o preenchimento automático).
"""

import base64
import json
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

#: Tipos aceitos nos uploads de CNH (JPEG/PNG/WebP — o que sai de celular).
TIPOS_DE_IMAGEM = {"image/jpeg", "image/png", "image/webp"}
TAMANHO_MAXIMO = 10 * 1024 * 1024  # 10 MB por foto

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
            "description": "false se as fotos não permitem ler os dados com segurança",
        },
    },
    "required": ["nome", "cpf", "cnh_numero", "cnh_categoria", "cnh_validade", "legivel"],
    "additionalProperties": False,
}

INSTRUCAO = (
    "As imagens são a frente e/ou o verso de uma CNH brasileira (Carteira Nacional "
    "de Habilitação), enviadas pelo próprio cadastro de um cliente de uma locadora. "
    "Extraia exatamente o que está impresso no documento, sem inventar: nome completo, "
    "CPF, número de registro da CNH, categoria e data de validade. Campo ilegível ou "
    "ausente = null. Se a imagem não for uma CNH ou estiver ilegível, marque "
    "legivel=false e deixe os campos null."
)


def disponivel():
    return bool(settings.ANTHROPIC_API_KEY)


def _bloco_imagem(arquivo):
    arquivo.seek(0)
    dados = base64.standard_b64encode(arquivo.read()).decode("utf-8")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": arquivo.content_type, "data": dados},
    }


def validar_upload(arquivo):
    """Mensagem de erro para o upload, ou None se ele serve."""
    if arquivo.content_type not in TIPOS_DE_IMAGEM:
        return "Envie a foto da CNH em JPEG, PNG ou WebP."
    if arquivo.size > TAMANHO_MAXIMO:
        return "Foto muito grande (máximo 10 MB) — tire uma foto menor."
    return None


def extrair_dados(fotos):
    """Lê as fotos da CNH e devolve o dict do ESQUEMA, ou None se indisponível/erro.

    `fotos`: lista de arquivos enviados (frente e/ou verso).
    """
    if not disponivel() or not fotos:
        return None
    import anthropic

    cliente = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    conteudo = [_bloco_imagem(foto) for foto in fotos]
    conteudo.append({"type": "text", "text": INSTRUCAO})
    try:
        resposta = cliente.messages.create(
            model=settings.CNH_MODELO,
            max_tokens=16000,
            output_config={"format": {"type": "json_schema", "schema": ESQUEMA}},
            messages=[{"role": "user", "content": conteudo}],
        )
    except anthropic.APIError as erro:
        logger.warning("Leitura de CNH falhou na API: %s", erro)
        return None
    if resposta.stop_reason == "refusal":
        logger.warning("Leitura de CNH recusada pela API (stop_reason=refusal).")
        return None
    texto = next((b.text for b in resposta.content if b.type == "text"), None)
    if not texto:
        return None
    try:
        return json.loads(texto)
    except ValueError:
        logger.warning("Leitura de CNH devolveu JSON inválido.")
        return None
