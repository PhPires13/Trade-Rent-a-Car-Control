"""Etapa 17 — frente sessão e autenticação: loop do login, fetch com sessão
expirada, páginas 400/403 em português, aviso de erro HTMX e Axes no Admin."""

import re

import pytest
from axes.models import AccessAttempt
from django.core.exceptions import PermissionDenied
from django.template import loader
from django.test import Client, RequestFactory, override_settings
from django.views import defaults

from config.middleware import SESSAO_EXPIRADA

# ---------- login ----------


def test_entrar_com_next_entrar_ja_logado_vai_ao_painel_sem_erro(usuario_logado):
    # antes: ValueError "Redirection loop" -> erro 500
    resposta = usuario_logado.get("/entrar/?next=/entrar/")
    assert resposta.status_code == 302
    assert resposta["Location"] == "/"


@pytest.mark.django_db
def test_login_com_next_entrar_vai_ao_painel(django_user_model):
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    resposta = Client().post(
        "/entrar/?next=/entrar/", {"username": "dono", "password": "senha-forte-123"}
    )
    assert resposta.status_code == 302
    assert resposta["Location"] == "/"


# ---------- fetch() com sessão expirada ----------


@pytest.mark.django_db
def test_fetch_sem_login_recebe_json_com_o_motivo():
    resposta = Client().post("/clientes/cnh/extrair/", HTTP_SEC_FETCH_MODE="cors")
    assert resposta.status_code == 401
    assert resposta.json() == {"erro": SESSAO_EXPIRADA}


@pytest.mark.django_db
def test_navegacao_normal_sem_login_continua_indo_ao_login():
    resposta = Client().get("/km/", HTTP_SEC_FETCH_MODE="navigate")
    assert resposta.status_code == 302
    assert resposta["Location"].startswith("/entrar/")


def test_fetch_logado_nao_e_afetado(usuario_logado):
    resposta = usuario_logado.get("/km/", HTTP_SEC_FETCH_MODE="cors")
    assert resposta.status_code == 200


# ---------- páginas de erro ----------


def test_400_em_portugues_sem_contexto():
    html = loader.get_template("400.html").render()
    assert "Não deu para abrir este endereço" in html
    assert 'href="/"' in html


@pytest.mark.django_db
def test_host_estranho_mostra_400_em_portugues():
    with override_settings(DEBUG=False):
        resposta = Client().get("/", HTTP_HOST="site-estranho.example")
    assert resposta.status_code == 400
    assert "Não deu para abrir este endereço" in resposta.content.decode()


@pytest.mark.django_db
def test_403_em_portugues_com_botao_de_voltar(django_user_model):
    request = RequestFactory().get("/")
    request.user = django_user_model(username="x")
    resposta = defaults.permission_denied(request, PermissionDenied("x"))
    html = resposta.content.decode()
    assert resposta.status_code == 403
    assert "Isto não pode ser feito por aqui" in html
    assert 'href="/"' in html


# ---------- aviso quando um clique HTMX falha ----------


def test_base_avisa_quando_htmx_falha(usuario_logado):
    html = usuario_logado.get("/").content.decode()
    assert "htmx:sendError" in html
    assert "htmx:responseError" in html
    assert "não foi salvo" in html


# ---------- Admin: bloqueios de login em português ----------


def test_admin_de_bloqueios_em_portugues(admin_client):
    tentativa = AccessAttempt.objects.create(
        username="paulo",
        ip_address="10.0.0.1",
        user_agent="celular",
        failures_since_start=6,
        get_data="",
        post_data="",
        http_accept="",
        path_info="/entrar/",
    )
    html = admin_client.get("/admin/axes/accessattempt/").content.decode()
    cabecalhos = " ".join(re.findall(r'<th scope="col"[^>]*>(.*?)</th>', html, re.S))
    for rotulo in (
        "Usuário digitado",
        "De onde (IP)",
        "Senhas erradas seguidas",
        "Última tentativa",
    ):
        assert rotulo in cabecalhos
    for ingles in ("Username", "IP Address", "Failed Logins", "User Agent", "Attempt Time"):
        assert ingles not in cabecalhos
    assert "paulo" in html
    # apagar a linha destrava o login
    resposta = admin_client.post(
        "/admin/axes/accessattempt/",
        {"action": "delete_selected", "_selected_action": [tentativa.pk], "post": "yes"},
    )
    assert resposta.status_code == 302
    assert not AccessAttempt.objects.exists()


def test_admin_de_bloqueios_nao_deixa_criar(admin_client):
    assert admin_client.get("/admin/axes/accessattempt/add/").status_code == 403


# ---------- revisão adversarial ----------


@pytest.mark.django_db
def test_login_com_next_sair_sem_barra_vai_ao_painel(django_user_model):
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    resposta = Client().post(
        "/entrar/?next=/sair", {"username": "dono", "password": "senha-forte-123"}
    )
    assert resposta["Location"] == "/"


def test_login_com_next_normal_continua_valendo(client, django_user_model):
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    resposta = client.post(
        "/entrar/?next=/km/", {"username": "dono", "password": "senha-forte-123"}
    )
    assert resposta["Location"] == "/km/"


def test_htmx_sem_login_nao_vira_json_401(db):
    resposta = Client().post(
        "/clientes/cnh/extrair/",
        HTTP_HX_REQUEST="true",
        HTTP_SEC_FETCH_MODE="cors",
        HTTP_HX_CURRENT_URL="http://testserver/clientes/novo/",
    )
    assert resposta.status_code == 204
    assert resposta["HX-Redirect"] == "/entrar/?next=/clientes/novo/"


def test_fetch_com_token_csrf_velho_recebe_json(usuario_logado):
    from config.middleware import PAGINA_VELHA

    cliente = Client(enforce_csrf_checks=True)
    cliente.force_login(_usuario(usuario_logado))
    resposta = cliente.post("/clientes/cnh/extrair/", HTTP_SEC_FETCH_MODE="cors")
    assert resposta.status_code == 403
    assert resposta.json() == {"erro": PAGINA_VELHA}


def test_formulario_com_token_csrf_velho_mostra_pagina_em_portugues(usuario_logado):
    cliente = Client(enforce_csrf_checks=True)
    cliente.force_login(_usuario(usuario_logado))
    resposta = cliente.post("/km/", HTTP_SEC_FETCH_MODE="navigate")
    assert resposta.status_code == 403
    assert "A página ficou aberta muito tempo" in resposta.content.decode()


def _usuario(cliente_logado):
    from django.contrib.auth import get_user_model

    pk = cliente_logado.session["_auth_user_id"]
    return get_user_model().objects.get(pk=pk)
