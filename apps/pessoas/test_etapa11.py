"""Etapa 11 — foto do motorista e leitura automática da CNH."""

import io
import json
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from apps.pessoas import cnh
from apps.pessoas.models import Cliente


def foto_cnh(nome="cnh.png"):
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(buffer, format="PNG")
    return SimpleUploadedFile(nome, buffer.getvalue(), content_type="image/png")


DADOS_LIDOS = {
    "nome": "Arlen Souza",
    "cpf": "111.222.333-44",
    "cnh_numero": "01234567890",
    "cnh_categoria": "B",
    "cnh_validade": "2030-05-10",
    "legivel": True,
}


def test_cadastro_com_foto_e_cnh(usuario_logado, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    resposta = usuario_logado.post(
        "/clientes/novo/",
        {
            "nome": "Arlen Souza",
            "cpf_cnpj": "111.222.333-44",
            "status": Cliente.Status.ATIVO,
            "foto": foto_cnh("rosto.png"),
            "cnh_frente": foto_cnh("frente.png"),
            "cnh_verso": foto_cnh("verso.png"),
        },
    )
    assert resposta.status_code == 302
    cliente = Cliente.objects.get(cpf_cnpj="111.222.333-44")
    assert cliente.foto.name.startswith("clientes/")
    assert cliente.cnh_frente.name.startswith("cnh/")
    assert cliente.cnh_verso.name.startswith("cnh/")


def test_extrair_devolve_dados_para_validacao(usuario_logado, settings):
    settings.ANTHROPIC_API_KEY = "chave-de-teste"
    with mock.patch("apps.pessoas.views.cnh.extrair_dados", return_value=dict(DADOS_LIDOS)):
        resposta = usuario_logado.post(
            "/clientes/cnh/extrair/", {"cnh_frente": foto_cnh(), "cnh_verso": foto_cnh()}
        )
    assert resposta.status_code == 200
    dados = json.loads(resposta.content)["dados"]
    assert dados["nome"] == "Arlen Souza"
    assert dados["cnh_validade"] == "2030-05-10"
    assert "legivel" not in dados
    # nada foi gravado — quem cadastra valida e salva depois
    assert Cliente.objects.count() == 0


def test_extrair_sem_chave_avisa_que_esta_desligado(usuario_logado, settings):
    settings.ANTHROPIC_API_KEY = ""
    resposta = usuario_logado.post("/clientes/cnh/extrair/", {"cnh_frente": foto_cnh()})
    assert resposta.status_code == 503


def test_extrair_sem_fotos_e_erro_claro(usuario_logado, settings):
    settings.ANTHROPIC_API_KEY = "chave-de-teste"
    resposta = usuario_logado.post("/clientes/cnh/extrair/", {})
    assert resposta.status_code == 400


def test_extrair_ilegivel_pede_outra_foto(usuario_logado, settings):
    settings.ANTHROPIC_API_KEY = "chave-de-teste"
    ilegivel = dict(DADOS_LIDOS, legivel=False)
    with mock.patch("apps.pessoas.views.cnh.extrair_dados", return_value=ilegivel):
        resposta = usuario_logado.post("/clientes/cnh/extrair/", {"cnh_frente": foto_cnh()})
    assert resposta.status_code == 422
    assert "legíveis" in json.loads(resposta.content)["erro"]


def test_extrair_aceita_pdf_da_cnh_digital(usuario_logado, settings):
    settings.ANTHROPIC_API_KEY = "chave-de-teste"
    pdf = SimpleUploadedFile("cnh.pdf", b"%PDF-1.4 x", content_type="application/pdf")
    with mock.patch("apps.pessoas.views.cnh.extrair_dados", return_value=dict(DADOS_LIDOS)) as m:
        resposta = usuario_logado.post("/clientes/cnh/extrair/", {"cnh_frente": pdf})
    assert resposta.status_code == 200
    m.assert_called_once()


def test_extrair_rejeita_arquivo_que_nao_e_imagem_nem_pdf(usuario_logado, settings):
    settings.ANTHROPIC_API_KEY = "chave-de-teste"
    texto = SimpleUploadedFile("cnh.txt", b"nao sou uma cnh", content_type="text/plain")
    resposta = usuario_logado.post("/clientes/cnh/extrair/", {"cnh_frente": texto})
    assert resposta.status_code == 400


def test_form_do_cliente_aceita_cnh_em_pdf(db, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    from apps.pessoas.views import ClienteForm

    pdf = SimpleUploadedFile("cnh.pdf", b"%PDF-1.4 x", content_type="application/pdf")
    form = ClienteForm(
        {"nome": "Arlen", "cpf_cnpj": "111.222.333-44", "status": Cliente.Status.ATIVO},
        {"cnh_frente": pdf},
    )
    assert form.is_valid(), form.errors


def test_extrair_dados_monta_a_chamada_certa(settings):
    """O serviço manda as imagens + instrução e devolve o JSON estruturado."""
    settings.ANTHROPIC_API_KEY = "chave-de-teste"
    resposta_api = mock.Mock(
        stop_reason="end_turn",
        content=[mock.Mock(type="text", text=json.dumps(DADOS_LIDOS))],
    )
    cliente_api = mock.Mock()
    cliente_api.messages.create.return_value = resposta_api
    with mock.patch("anthropic.Anthropic", return_value=cliente_api) as construtor:
        dados = cnh.extrair_dados([foto_cnh()])
    assert dados == DADOS_LIDOS
    construtor.assert_called_once_with(api_key="chave-de-teste")
    chamada = cliente_api.messages.create.call_args.kwargs
    assert chamada["model"] == settings.CNH_MODELO
    assert chamada["output_config"]["format"]["type"] == "json_schema"
    blocos = chamada["messages"][0]["content"]
    assert blocos[0]["type"] == "image"
    assert blocos[-1]["type"] == "text"


def test_extrair_dados_recusa_vira_none(settings):
    settings.ANTHROPIC_API_KEY = "chave-de-teste"
    resposta_api = mock.Mock(stop_reason="refusal", content=[])
    cliente_api = mock.Mock()
    cliente_api.messages.create.return_value = resposta_api
    with mock.patch("anthropic.Anthropic", return_value=cliente_api):
        assert cnh.extrair_dados([foto_cnh()]) is None


def test_foto_do_cliente_aparece_no_hub(usuario_logado, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    cliente = Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")
    cliente.foto = foto_cnh("rosto.png")
    cliente.save()
    html = usuario_logado.get("/clientes/").content.decode()
    assert cliente.foto.url in html


def test_cnh_do_cliente_nao_e_publica(client, settings, tmp_path, db):
    settings.MEDIA_ROOT = tmp_path
    cliente = Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")
    cliente.cnh_frente = foto_cnh("frente.png")
    cliente.save()
    resposta = client.get(cliente.cnh_frente.url)
    assert resposta.status_code == 302  # documento sensível: só logado
