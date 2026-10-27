"""Etapa 17 (achados extras), área global: valor ambíguo, IDs com dígito não ASCII,
limite de upload no Admin, foco do teclado, filtros e termo "Status"."""

from pathlib import Path

import pytest
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory
from django.urls import reverse

from apps.campos import TAMANHO_MAXIMO_UPLOAD, CampoDinheiro, dinheiro_br, id_valido
from apps.financeiro.models import NotaDebito
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente

RAIZ = Path(__file__).resolve().parents[1]


# --- "12,000" não vira R$ 12,00 em silêncio ---------------------------------------


@pytest.mark.parametrize("texto", ["12,000", "1,500", "-12,000", "R$ 12,000", "1,250.00"])
def test_valor_ambiguo_e_recusado_com_explicacao(texto):
    with pytest.raises(ValidationError) as erro:
        dinheiro_br(texto)
    assert erro.value.code == "valor_ambiguo"
    assert "12.000,00" in erro.value.messages[0]


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("12,00", "12.00"),
        ("1.250,50", "1250.50"),
        ("12.000", "12000"),
        ("12,5", "12.5"),
        ("1.250,000", "1250.000"),
        ("650.00", "650.00"),
    ],
)
def test_valores_sem_ambiguidade_continuam_aceitos(texto, esperado):
    assert str(dinheiro_br(texto)) == esperado


def test_campo_dinheiro_mostra_a_explicacao():
    with pytest.raises(ValidationError) as erro:
        CampoDinheiro(max_digits=10, decimal_places=2).clean("12,000")
    assert "ambíguo" in erro.value.messages[0]


@pytest.mark.django_db
def test_nd_com_valor_ambiguo_nao_e_emitida(usuario_logado):
    cliente = Cliente.objects.create(nome="Cliente Um", cpf_cnpj="529.982.247-25")
    resposta = usuario_logado.post(
        reverse("financeiro:nd_nova"),
        {
            "cliente": cliente.pk,
            "item_descricao": ["Teste"],
            "item_valor": ["12,000"],
            "data_emissao": "2026-10-20",
            "vencimento": "2026-11-05",
            "token": "t1",
        },
        follow=True,
    )
    assert not NotaDebito.objects.exists()
    assert "ambíguo" in resposta.content.decode()


# --- IDs com "²" na URL não dão erro 500 -------------------------------------------


@pytest.mark.parametrize("texto", ["²", "١٢", "1234567890", "", "abc"])
def test_id_valido_recusa_o_que_int_nao_le(texto):
    assert not id_valido(texto)


def test_id_valido_aceita_numero_comum():
    assert id_valido("42")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "url",
    [
        "/alocacoes/nova/?veiculo=²",
        "/alocacoes/nova/?cliente=²",
        "/manutencao/registrar/?veiculo=²",
        "/multas/?cliente=²",
        "/multas/gerar-nd/?cliente=²",
        "/multas/nova/?veiculo=²&cliente=²",
        "/multas/quem-estava/?veiculo=²&data_infracao=2026-01-01",
        "/sinistros/quem-estava/?veiculo=²&data=2026-01-01",
    ],
)
def test_id_com_digito_nao_ascii_nao_da_erro_500(usuario_logado, url):
    resposta = usuario_logado.get(url)
    assert resposta.status_code < 500


# --- Admin respeita o limite de 10 MB dos arquivos ---------------------------------


def _arquivo_grande(nome):
    return SimpleUploadedFile(nome, b"0" * (TAMANHO_MAXIMO_UPLOAD + 1), "application/pdf")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("modelo", "campo"),
    [(Cliente, "cnh_frente"), (Cliente, "cnh_verso"), (Veiculo, "documento")],
)
def test_admin_recusa_arquivo_acima_de_10mb(admin_user, modelo, campo):
    requisicao = RequestFactory().get("/")
    requisicao.user = admin_user
    modelo_admin = admin.site._registry[modelo]
    formulario = modelo_admin.get_form(requisicao)(
        data={}, files={campo: _arquivo_grande("doc.pdf")}
    )
    assert not formulario.is_valid()
    assert "10 MB" in " ".join(formulario.errors.get(campo, []))


@pytest.mark.parametrize(
    ("modelo", "campo"),
    [
        (Cliente, "foto"),
        (Cliente, "cnh_frente"),
        (Cliente, "cnh_verso"),
        (Veiculo, "foto"),
        (Veiculo, "documento"),
    ],
)
def test_modelo_tem_o_limite_de_upload(modelo, campo):
    from apps.campos import validar_tamanho_arquivo

    assert validar_tamanho_arquivo in modelo._meta.get_field(campo).validators


# --- teclado: foco visível, pular para o conteúdo, filtros -------------------------


@pytest.mark.django_db
def test_pagina_tem_pular_para_o_conteudo_e_foco_visivel(usuario_logado):
    html = usuario_logado.get(reverse("painel")).content.decode()
    assert 'href="#conteudo"' in html and "Pular para o conteúdo" in html
    assert '<main id="conteudo"' in html
    assert ":focus-visible{outline:3px solid" in html


def test_links_do_conteudo_tem_area_de_toque_de_44px():
    base = (RAIZ / "templates/base.html").read_text()
    assert "height:max(44px,100%)" in base


def test_filtro_automatico_espera_enter_quando_muda_pelo_teclado():
    ux = (RAIZ / "static/js/ux.js").read_text()
    assert "FILTRO_AUTO" in ux and "stopPropagation" in ux and "ux-foco-filtro" in ux


# --- "Situação" no lugar de "Status" -----------------------------------------------


@pytest.mark.django_db
def test_sinistros_e_preventivas_falam_situacao(usuario_logado):
    html = usuario_logado.get(reverse("sinistros:lista")).content.decode()
    assert "Todas as situações" in html and "Todos os status" not in html
    preventivas = (RAIZ / "templates/manutencao/preventivas.html").read_text()
    assert ">Status<" not in preventivas
