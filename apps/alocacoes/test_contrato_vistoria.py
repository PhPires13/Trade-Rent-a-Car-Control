"""Contrato de locação e checklist de vistoria (etapa 11+)."""

import io
import json
from datetime import date
from decimal import Decimal
from unittest import mock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from apps.alocacoes.models import Alocacao, Vistoria
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente


def foto(nome="checklist.png"):
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(buffer, format="PNG")
    return SimpleUploadedFile(nome, buffer.getvalue(), content_type="image/png")


@pytest.fixture
def alocacao(db):
    veiculo = Veiculo.objects.create(
        placa="QXQ6C10", marca_modelo="VW/Gol 1.0", renavam="123456789", km_atual=50_000
    )
    cliente = Cliente.objects.create(
        nome="Arlen Souza",
        cpf_cnpj="111.222.333-44",
        cnh_numero="01234567890",
        endereco="Rua A, 10 — BH/MG",
    )
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        caucao_valor=Decimal("1000.00"),
        km_entrega=50_000,
        limite_km=Alocacao.LimiteKm.LIMITADO,
        franquia_km_mensal=3_000,
        taxa_km_excedido=Decimal("0.50"),
    )


# ---------- contrato ----------


def test_contrato_traz_partes_veiculo_e_valores(usuario_logado, alocacao):
    html = usuario_logado.get(f"/alocacoes/{alocacao.pk}/contrato/").content.decode()
    assert "Contrato de Locação" in html
    assert "Arlen Souza" in html and "111.222.333-44" in html
    assert "QXQ6C10" in html and "VW/Gol 1.0" in html
    assert "R$ 650,00" in html or "650,00" in html.replace("&nbsp;", " ") or "650.00" in html
    assert "1.000,00" in html or "1000.00" in html  # caução
    assert "3.000 km" in html or "3000" in html  # franquia (limitado)
    assert "0.50" in html or "0,50" in html  # taxa por km


def test_contrato_ilimitado_nao_fala_de_franquia(usuario_logado, db):
    veiculo = Veiculo.objects.create(placa="RNB9J66", marca_modelo="Voyage")
    cliente = Cliente.objects.create(nome="Beto", cpf_cnpj="222.333.444-55")
    alocacao = Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("750.00"),
        km_entrega=0,
    )
    html = usuario_logado.get(f"/alocacoes/{alocacao.pk}/contrato/").content.decode()
    assert "quilometragem livre" in html
    assert "franquia de" not in html


def test_criar_alocacao_oferece_o_contrato(usuario_logado, db):
    Veiculo.objects.create(placa="TTT1A11", marca_modelo="Gol")
    cliente = Cliente.objects.create(nome="Caio", cpf_cnpj="333.444.555-66")
    veiculo = Veiculo.objects.get(placa="TTT1A11")
    resposta = usuario_logado.post(
        "/alocacoes/nova/",
        {
            "veiculo": veiculo.pk,
            "cliente": cliente.pk,
            "data_inicio": "2026-07-01",
            "valor_semanal": "650.00",
            "km_entrega": 0,
            "limite_km": "ilimitado",
        },
        follow=True,
    )
    html = resposta.content.decode()
    assert "Gerar contrato de locação" in html


# ---------- checklist para imprimir ----------


def test_checklist_em_branco_imprime_itens_e_dados(usuario_logado, alocacao):
    html = usuario_logado.get(f"/alocacoes/{alocacao.pk}/vistoria/imprimir/").content.decode()
    assert "Checklist de vistoria" in html
    assert "QXQ6C10" in html and "Arlen Souza" in html
    assert "Para-choque dianteiro" in html and "Pneus e rodas" in html
    assert "Combustível" in html


# ---------- vistoria: salvar e ler a foto ----------


def test_salvar_vistoria_atualiza_km_do_veiculo(usuario_logado, alocacao, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/vistoria/nova/",
        {
            "tipo": "entrada",
            "data": "2026-07-01",
            "km": 50_100,
            "combustivel": "meio",
            "avarias": "Risco na porta direita",
            "notas": "Entregue com tapetes",
            "foto": foto(),
        },
    )
    assert resposta.status_code == 302
    vistoria = Vistoria.objects.get()
    assert vistoria.alocacao == alocacao
    assert vistoria.foto.name.startswith("vistorias/")
    alocacao.veiculo.refresh_from_db()
    assert alocacao.veiculo.km_atual == 50_100  # km da vistoria puxa o odômetro


DADOS_CHECKLIST = {
    "tipo": "saida",
    "data": "2026-08-01",
    "km": 52_300,
    "combustivel": "um_quarto",
    "avarias": "Para-choque traseiro: amassado leve",
    "notas": "Devolvido sem estepe",
    "legivel": True,
}


def test_extrair_checklist_devolve_dados(usuario_logado, settings, db):
    settings.ANTHROPIC_API_KEY = "chave-de-teste"
    with mock.patch(
        "apps.alocacoes.views.checklist.extrair_dados", return_value=dict(DADOS_CHECKLIST)
    ):
        resposta = usuario_logado.post("/alocacoes/vistoria/extrair/", {"foto": foto()})
    assert resposta.status_code == 200
    dados = json.loads(resposta.content)["dados"]
    assert dados["km"] == 52_300
    assert dados["combustivel"] == "um_quarto"
    assert Vistoria.objects.count() == 0  # nada gravado sem validação humana


def test_extrair_checklist_ilegivel_pede_outra_foto(usuario_logado, settings, db):
    settings.ANTHROPIC_API_KEY = "chave-de-teste"
    ilegivel = dict(DADOS_CHECKLIST, legivel=False)
    with mock.patch("apps.alocacoes.views.checklist.extrair_dados", return_value=ilegivel):
        resposta = usuario_logado.post("/alocacoes/vistoria/extrair/", {"foto": foto()})
    assert resposta.status_code == 422


def test_links_na_lista_de_alocacoes(usuario_logado, alocacao):
    html = usuario_logado.get("/alocacoes/").content.decode()
    assert f"/alocacoes/{alocacao.pk}/contrato/" in html
    assert f"/alocacoes/{alocacao.pk}/vistoria/imprimir/" in html
    assert f"/alocacoes/{alocacao.pk}/vistoria/nova/" in html
