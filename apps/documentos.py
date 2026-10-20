"""Leitura de documentos (foto ou PDF) com a API do Claude — base comum.

Usada pela CNH no cadastro de motorista (apps/pessoas/cnh.py) e pelo CRLV no
cadastro de veículo (apps/frota/crlv.py). Os dados extraídos preenchem o
formulário como SUGESTÃO — quem cadastra valida e corrige antes de salvar.

Sem ANTHROPIC_API_KEY configurada, o recurso fica desligado e os cadastros
funcionam normalmente (só sem o preenchimento automático).
"""

import base64
import io
import json
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

#: Foto de celular (JPEG/PNG/WebP) ou PDF (CNH digital / CRLV-e).
TIPOS_DE_IMAGEM = {"image/jpeg", "image/png", "image/webp"}
TIPOS_ACEITOS = TIPOS_DE_IMAGEM | {"application/pdf"}
TAMANHO_MAXIMO = 10 * 1024 * 1024  # 10 MB por arquivo

#: A API recusa imagem acima de 5 MB (em base64, que cresce 4/3) ou de 8000 px
#: de lado. Foto acima disso é reduzida antes de enviar (C18).
LIMITE_IMAGEM_API = 3_700_000
LADO_MAXIMO_API = 8000
#: Lado da foto reduzida — sobra resolução para ler CNH/CRLV.
LADO_REDUZIDO = 2400

#: Tempo máximo de espera pela API: o servidor derruba a página com 120 s
#: (Procfile); 50 s + 1 nova tentativa cabem nisso (C19).
TEMPO_LIMITE_API = 50
TENTATIVAS_EXTRAS_API = 1


def disponivel():
    return bool(settings.ANTHROPIC_API_KEY)


def tipo_real(arquivo):
    """Tipo pelo conteúdo (bytes iniciais), não pelo nome nem pelo navegador.

    JPEG salvo como .png pelo WhatsApp é JPEG — mandar o tipo errado faz a API
    recusar a leitura. Devolve None se não for foto aceita nem PDF.
    """
    arquivo.seek(0)
    inicio = arquivo.read(12)
    arquivo.seek(0)
    if inicio.startswith(b"%PDF"):
        return "application/pdf"
    if inicio.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if inicio.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if inicio[:4] == b"RIFF" and inicio[8:12] == b"WEBP":
        return "image/webp"
    return None


def _imagem_para_api(arquivo, tipo):
    """(bytes, media_type) da foto, reduzida se passar do limite da API."""
    arquivo.seek(0)
    dados = arquivo.read()
    arquivo.seek(0)
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(dados)) as imagem:
        grande_demais = len(dados) > LIMITE_IMAGEM_API or max(imagem.size) > LADO_MAXIMO_API
        if not grande_demais:
            return dados, tipo
        imagem = ImageOps.exif_transpose(imagem)  # foto de celular "deitada" fica de pé
        imagem.thumbnail((LADO_REDUZIDO, LADO_REDUZIDO))
        saida = io.BytesIO()
        imagem.convert("RGB").save(saida, format="JPEG", quality=85)
    return saida.getvalue(), "image/jpeg"


def bloco_do_arquivo(arquivo):
    """Bloco da API para o upload: `document` para PDF, `image` para foto."""
    tipo = tipo_real(arquivo)
    if tipo == "application/pdf":
        arquivo.seek(0)
        dados = arquivo.read()
        arquivo.seek(0)
        return {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.standard_b64encode(dados).decode("utf-8"),
            },
        }
    dados, tipo = _imagem_para_api(arquivo, tipo or arquivo.content_type)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": tipo,
            "data": base64.standard_b64encode(dados).decode("utf-8"),
        },
    }


def validar_upload(arquivo):
    """Mensagem de erro para o upload, ou None se ele serve.

    O tipo é conferido pelo conteúdo, e a foto precisa abrir de verdade —
    assim um arquivo com problema recebe a explicação certa em vez do
    "tente de novo" da falha da API, que nunca resolveria.
    """
    tipo = tipo_real(arquivo)
    if tipo is None:
        return (
            "Este arquivo não é uma foto JPEG, PNG ou WebP nem um PDF. "
            "Foto de iPhone (HEIC): tire um print da foto ou envie como JPEG."
        )
    if arquivo.size > TAMANHO_MAXIMO:
        return "Arquivo muito grande (máximo 10 MB) — tire uma foto menor ou reexporte o PDF."
    if tipo != "application/pdf":
        from PIL import Image

        try:
            arquivo.seek(0)
            with Image.open(arquivo) as imagem:
                # load() decodifica a foto inteira: o verify() não pega JPEG
                # cortado no meio (upload interrompido), que depois quebraria
                # a redução da foto grande com erro 500
                imagem.load()
        except Exception:  # noqa: BLE001 — qualquer falha do Pillow = foto estragada
            return "Não consegui abrir esta foto — ela parece corrompida. Tire outra foto."
        finally:
            arquivo.seek(0)
    return None


def extrair_estruturado(arquivos, esquema, instrucao, rotulo):
    """Lê os arquivos e devolve o dict do esquema, ou None se indisponível/erro."""
    if not disponivel() or not arquivos:
        return None
    import anthropic

    # sem limite o SDK espera até 10 min com 2 novas tentativas — o servidor
    # derrubaria a página antes, com a leitura já cobrada (C19)
    cliente = anthropic.Anthropic(
        api_key=settings.ANTHROPIC_API_KEY,
        timeout=TEMPO_LIMITE_API,
        max_retries=TENTATIVAS_EXTRAS_API,
    )
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
