"""Leitura de documentos (foto ou PDF) com a API do Claude — base comum.

Usada pela CNH no cadastro de motorista (apps/pessoas/cnh.py) e pelo CRLV no
cadastro de veículo (apps/frota/crlv.py). Os dados extraídos preenchem o
formulário como SUGESTÃO — quem cadastra valida e corrige antes de salvar.

Sem ANTHROPIC_API_KEY configurada, o recurso fica desligado e os cadastros
funcionam normalmente (só sem o preenchimento automático).
"""

import base64
import json
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

#: Foto de celular (JPEG/PNG/WebP) ou PDF (CNH digital / CRLV-e).
TIPOS_DE_IMAGEM = {"image/jpeg", "image/png", "image/webp"}
TIPOS_ACEITOS = TIPOS_DE_IMAGEM | {"application/pdf"}
TAMANHO_MAXIMO = 10 * 1024 * 1024  # 10 MB por arquivo


def disponivel():
    return bool(settings.ANTHROPIC_API_KEY)


def _e_pdf(arquivo):
    """Detecta PDF pelo conteúdo (%PDF), não só pelo content_type do navegador."""
    arquivo.seek(0)
    inicio = arquivo.read(5)
    arquivo.seek(0)
    return inicio.startswith(b"%PDF") or arquivo.content_type == "application/pdf"


def bloco_do_arquivo(arquivo):
    """Bloco da API para o upload: `document` para PDF, `image` para foto."""
    arquivo.seek(0)
    dados = base64.standard_b64encode(arquivo.read()).decode("utf-8")
    if _e_pdf(arquivo):
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": dados},
        }
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": arquivo.content_type, "data": dados},
    }


def validar_upload(arquivo):
    """Mensagem de erro para o upload, ou None se ele serve."""
    if arquivo.content_type not in TIPOS_ACEITOS and not _e_pdf(arquivo):
        return "Envie o documento como foto (JPEG, PNG ou WebP) ou PDF."
    if arquivo.size > TAMANHO_MAXIMO:
        return "Arquivo muito grande (máximo 10 MB) — tire uma foto menor ou reexporte o PDF."
    return None


def extrair_estruturado(arquivos, esquema, instrucao, rotulo):
    """Lê os arquivos e devolve o dict do esquema, ou None se indisponível/erro."""
    if not disponivel() or not arquivos:
        return None
    import anthropic

    cliente = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    conteudo = [bloco_do_arquivo(arquivo) for arquivo in arquivos]
    conteudo.append({"type": "text", "text": instrucao})
    try:
        resposta = cliente.messages.create(
            model=settings.CNH_MODELO,
            max_tokens=16000,
            output_config={"format": {"type": "json_schema", "schema": esquema}},
            messages=[{"role": "user", "content": conteudo}],
        )
    except anthropic.APIError as erro:
        logger.warning("Leitura de %s falhou na API: %s", rotulo, erro)
        return None
    if resposta.stop_reason == "refusal":
        logger.warning("Leitura de %s recusada pela API (stop_reason=refusal).", rotulo)
        return None
    texto = next((b.text for b in resposta.content if b.type == "text"), None)
    if not texto:
        return None
    try:
        return json.loads(texto)
    except ValueError:
        logger.warning("Leitura de %s devolveu JSON inválido.", rotulo)
        return None
