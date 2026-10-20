"""Bugs da frente infra: leitura automática de documentos (C18, C19)."""

import base64
import io
import os
from pathlib import Path
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from apps import documentos
from apps.pessoas import cnh


def _upload(imagem, formato, nome, content_type):
    buffer = io.BytesIO()
    imagem.save(buffer, format=formato)
    return SimpleUploadedFile(nome, buffer.getvalue(), content_type=content_type)


def test_c18_foto_grande_e_reduzida_antes_de_ir_para_a_api():
    """Foto de celular de ~6 MB: a API recusa acima de 5 MB; agora vai reduzida."""
    ruido = Image.frombytes("RGB", (1400, 1400), os.urandom(1400 * 1400 * 3))
    foto = _upload(ruido, "PNG", "cnh.png", "image/png")
    assert foto.size > documentos.LIMITE_IMAGEM_API
    assert documentos.validar_upload(foto) is None
    bloco = documentos.bloco_do_arquivo(foto)
    enviado = base64.standard_b64decode(bloco["source"]["data"])
    assert len(bloco["source"]["data"]) < 5 * 1024 * 1024
    assert bloco["source"]["media_type"] == "image/jpeg"
    assert enviado.startswith(b"\xff\xd8\xff")


def test_c18_foto_com_lado_acima_de_8000px_e_reduzida():
    foto = _upload(Image.new("RGB", (8200, 20), "white"), "PNG", "cnh.png", "image/png")
    bloco = documentos.bloco_do_arquivo(foto)
    enviado = Image.open(io.BytesIO(base64.standard_b64decode(bloco["source"]["data"])))
    assert max(enviado.size) <= documentos.LADO_REDUZIDO


def test_c18_foto_pequena_vai_sem_mexer():
    foto = _upload(Image.new("RGB", (50, 30), "white"), "PNG", "cnh.png", "image/png")
    original = foto.read()
    bloco = documentos.bloco_do_arquivo(foto)
    assert base64.standard_b64decode(bloco["source"]["data"]) == original
    assert bloco["source"]["media_type"] == "image/png"


def test_c18_tipo_vem_do_conteudo_e_nao_do_nome():
    """JPEG salvo como .png pelo WhatsApp: a API recusava por tipo divergente."""
    foto = _upload(Image.new("RGB", (50, 30), "white"), "JPEG", "cnh.png", "image/png")
    assert documentos.validar_upload(foto) is None
    assert documentos.bloco_do_arquivo(foto)["source"]["media_type"] == "image/jpeg"


def test_c18_arquivo_que_nao_e_foto_tem_mensagem_clara():
    falso = SimpleUploadedFile("cnh.jpg", b"isto nao e uma foto", content_type="image/jpeg")
    assert "não é uma foto" in documentos.validar_upload(falso)


def test_c18_foto_corrompida_nao_manda_tentar_de_novo():
    estragada = SimpleUploadedFile(
        "cnh.jpg", b"\xff\xd8\xff\xe0" + b"lixo" * 50, content_type="image/jpeg"
    )
    assert "corrompida" in documentos.validar_upload(estragada)


def test_c19_chamada_a_api_tem_tempo_limite_e_uma_tentativa_extra(settings):
    settings.ANTHROPIC_API_KEY = "chave-de-teste"
    foto = _upload(Image.new("RGB", (50, 30), "white"), "PNG", "cnh.png", "image/png")
    cliente_api = mock.Mock()
    cliente_api.messages.create.return_value = mock.Mock(stop_reason="end_turn", content=[])
    with mock.patch("anthropic.Anthropic", return_value=cliente_api) as construtor:
        cnh.extrair_dados([foto])
    kwargs = construtor.call_args.kwargs
    assert kwargs["timeout"] <= 60
    assert kwargs["max_retries"] <= 1


def test_c19_servidor_tem_mais_de_um_processo_e_timeout_maior_que_a_leitura():
    procfile = (Path(__file__).resolve().parents[2] / "Procfile").read_text()
    assert "--workers" in procfile
    assert "--timeout 120" in procfile
    # 2 chamadas de até TEMPO_LIMITE_API cabem no timeout do servidor
    assert documentos.TEMPO_LIMITE_API * (1 + documentos.TENTATIVAS_EXTRAS_API) < 120


def test_revisao_foto_cortada_no_meio_tem_mensagem_clara_e_nao_da_erro_500():
    """Foto que chegou pela metade (4G caiu): o verify() do Pillow não pega JPEG
    cortado, e a redução da foto grande quebrava com erro 500."""
    ruido = Image.frombytes("RGB", (1800, 1800), os.urandom(1800 * 1800 * 3))
    buffer = io.BytesIO()
    ruido.save(buffer, format="JPEG", quality=100)
    cortada = buffer.getvalue()[: documentos.LIMITE_IMAGEM_API + 100_000]
    foto = SimpleUploadedFile("cnh.jpg", cortada, content_type="image/jpeg")
    assert "corrompida" in documentos.validar_upload(foto)
