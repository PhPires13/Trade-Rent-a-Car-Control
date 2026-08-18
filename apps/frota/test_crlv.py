"""Leitura automática do CRLV no cadastro de veículo (etapa 11+)."""

import io
import json
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from apps.frota.models import Veiculo


def foto(nome="crlv.png"):
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(buffer, format="PNG")
    return SimpleUploadedFile(nome, buffer.getvalue(), content_type="image/png")


def pdf(nome="crlv.pdf"):
    return SimpleUploadedFile(nome, b"%PDF-1.4 conteudo", content_type="application/pdf")


DADOS_CRLV = {
    "placa": "RNB9J66",
    "renavam": "00123456789",
    "chassi": "9BWZZZ377VT004251",
    "marca_modelo": "VW/VOYAGE 1.6",
    "ano": "2020/2021",
    "legivel": True,
}


def test_extrair_crlv_devolve_dados(usuario_logado, settings, db):
    settings.ANTHROPIC_API_KEY = "chave-de-teste"
    with mock.patch("apps.frota.views.crlv.extrair_dados", return_value=dict(DADOS_CRLV)):
        resposta = usuario_logado.post("/frota/crlv/extrair/", {"documento": foto()})
    assert resposta.status_code == 200
    dados = json.loads(resposta.content)["dados"]
    assert dados["placa"] == "RNB9J66"
    assert dados["chassi"] == "9BWZZZ377VT004251"
    assert Veiculo.objects.count() == 0  # só sugestão — nada gravado


def test_extrair_crlv_aceita_pdf(usuario_logado, settings, db):
    settings.ANTHROPIC_API_KEY = "chave-de-teste"
    with mock.patch("apps.frota.views.crlv.extrair_dados", return_value=dict(DADOS_CRLV)) as m:
        resposta = usuario_logado.post("/frota/crlv/extrair/", {"documento": pdf()})
    assert resposta.status_code == 200
    m.assert_called_once()


def test_extrair_crlv_sem_chave_e_503(usuario_logado, settings, db):
    settings.ANTHROPIC_API_KEY = ""
    resposta = usuario_logado.post("/frota/crlv/extrair/", {"documento": foto()})
    assert resposta.status_code == 503


def test_pdf_vira_bloco_document_na_api(settings):
    """PDF vai como bloco `document` (não `image`) na chamada — exigência da API."""
    from apps.documentos import bloco_do_arquivo

    bloco = bloco_do_arquivo(pdf())
    assert bloco["type"] == "document"
    assert bloco["source"]["media_type"] == "application/pdf"
    bloco = bloco_do_arquivo(foto())
    assert bloco["type"] == "image"


def test_documento_do_veiculo_aceita_pdf_no_form(db):
    from apps.frota.views import VeiculoForm

    form = VeiculoForm(
        {
            "placa": "RNB9J66",
            "marca_modelo": "Voyage",
            "uso": Veiculo.Uso.LOCACAO,
            "km_atual": 0,
            "chave_reserva": Veiculo.ChaveReserva.DUVIDA,
        },
        {"documento": pdf()},
    )
    assert form.is_valid(), form.errors
    assert form.save().documento.name.startswith("veiculos/documentos/")
