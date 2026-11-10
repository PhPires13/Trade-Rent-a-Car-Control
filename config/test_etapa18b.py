"""Etapa 18b, área global: sessão expirada, toque no celular, teclado, datas e histórico."""

from pathlib import Path

import pytest
from django import forms
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.campos import MENSAGEM_DATA_INVALIDA, FormularioBR
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente
from config.test_navegador_17 import _alocar, _carro

RAIZ = Path(__file__).resolve().parents[1]
BASE = (RAIZ / "templates" / "base.html").read_text()
UX = (RAIZ / "static" / "js" / "ux.js").read_text()


# ---------- sessão expirada ao salvar ----------


@pytest.mark.django_db
def test_formulario_enviado_sem_sessao_avisa_no_login(client):
    resposta = client.post(reverse("multas:nova"), {"qualquer": "coisa"})

    assert resposta.status_code == 302
    assert "next=" in resposta["Location"]
    assert "expirou=1" in resposta["Location"]
    pagina = client.get(resposta["Location"]).content.decode()
    assert "Sua sessão expirou — o que você digitou NÃO foi salvo." in pagina


@pytest.mark.django_db
def test_abrir_tela_sem_sessao_nao_fala_em_sessao_expirada(client):
    resposta = client.get(reverse("multas:nova"))
    assert "expirou" not in resposta["Location"]
    pagina = client.get(resposta["Location"]).content.decode()
    assert "o que você digitou" not in pagina


@pytest.mark.django_db
def test_sair_sem_sessao_nao_fala_em_sessao_expirada(client):
    resposta = client.post(reverse("logout"))
    assert "expirou" not in resposta.get("Location", "")


@pytest.mark.django_db
def test_depois_de_entrar_volta_para_a_tela_sem_o_aviso(client, django_user_model):
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    destino = client.post(reverse("multas:nova"))["Location"]

    resposta = client.post(destino, {"username": "dono", "password": "senha-forte-123"})

    assert resposta.status_code == 302
    assert resposta["Location"] == reverse("multas:nova")


def test_sessao_vale_30_dias_desde_o_ultimo_uso():
    assert settings.SESSION_COOKIE_AGE == 60 * 60 * 24 * 30
    assert "config.middleware.SessaoRenovadaMiddleware" in settings.MIDDLEWARE


def test_sessao_e_renovada_uma_vez_por_dia_de_uso(client, django_user_model):
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    client.post(reverse("login"), {"username": "dono", "password": "senha-forte-123"})
    hoje = timezone.localdate().isoformat()
    assert client.session["renovada_em"] == hoje

    sessao = client.session
    sessao["renovada_em"] = "2000-01-01"  # último uso foi outro dia
    sessao.save()
    client.get(reverse("painel"))
    assert client.session["renovada_em"] == hoje


@pytest.mark.django_db
def test_login_com_pagina_velha_nao_fala_em_sessao_expirada():
    from django.test import Client

    navegador = Client(enforce_csrf_checks=True)
    url = reverse("login") + "?next=/frota/"
    navegador.get(url)
    resposta = navegador.post(url, {"username": "x", "password": "y"})
    assert "expirou" not in resposta["Location"]


# ---------- datas sem ano nos formulários ----------


class _FormData(FormularioBR, forms.Form):
    quando = forms.DateField()


def test_formulario_aceita_data_sem_ano_igual_ao_receber():
    hoje = timezone.localdate()
    form = _FormData(data={"quando": hoje.strftime("%d/%m")})
    assert form.is_valid(), form.errors
    assert form.cleaned_data["quando"] == hoje


def test_formulario_com_data_invalida_explica_o_que_fazer():
    form = _FormData(data={"quando": "31/31"})
    assert not form.is_valid()
    assert form.errors["quando"] == [MENSAGEM_DATA_INVALIDA]


def test_formulario_continua_aceitando_data_do_calendario():
    form = _FormData(data={"quando": "2026-10-27"})
    assert form.is_valid(), form.errors


# ---------- toque no celular: camada de 44px não rouba o vizinho ----------


def test_camada_de_toque_respeita_o_limite_calculado_pelo_ux_js():
    assert "var(--alvo-cima,22px)" in BASE
    assert "var(--alvo-baixo,22px)" in BASE
    assert "height:max(44px,100%)" not in BASE
    assert "function limitarAlvosDeToque" in UX
    # o seletor do JS é o mesmo da regra do CSS
    seletor = (
        "main a:not([data-alvo-livre]):not(.dica-botao):not(.alvo-toque)"
        ":not(.absolute):not(.fixed):not(.sticky)"
    )
    assert seletor + "::after" in BASE
    assert f'"{seletor}"' in UX


def test_painel_lista_inadimplentes_um_por_linha_com_alvo_de_toque(usuario_logado):
    Cliente.objects.create(
        nome="Bruno Henrique Lima", cpf_cnpj="1", status=Cliente.Status.INADIMPLENTE
    )
    html = usuario_logado.get(reverse("painel")).content.decode()
    assert 'class="alvo-toque underline hover:text-marca-preto"' in html
    assert '<li><a class="alvo-toque' in html


# ---------- teclado no cabeçalho e no "+ Lançar" ----------


def test_esc_fecha_menus_e_devolve_o_foco():
    assert '@keydown.escape.window="if (conta) { conta = false; $refs.botaoConta.focus() }' in BASE
    assert 'x-ref="botaoConta"' in BASE and 'x-ref="botaoMenu"' in BASE
    assert "lancar = false; $refs.botaoLancar.focus()" in BASE
    assert 'x-ref="botaoLancar"' in BASE


def test_botao_da_conta_tem_nome_para_leitor_de_tela(usuario_logado):
    html = usuario_logado.get(reverse("painel")).content.decode()
    assert 'aria-label="Conta: dono — sair ou Retaguarda"' in html


def test_menu_lancar_mais_largo_e_com_dica_em_linha_propria(usuario_logado):
    html = usuario_logado.get(reverse("painel")).content.decode()
    menu = html.split('id="menu-lancar"', 1)[1].split("</nav>", 1)[0]
    assert "w-72 max-w-[calc(100vw-1rem)]" in menu
    assert "(escolha o carro)" not in menu
    assert menu.count('<span class="block text-xs font-normal text-gray-600">escolha o carro') == 2


# ---------- dica técnica não aparece para leigos ----------


def test_formularios_nao_mostram_dica_tecnica_da_chave_de_api(usuario_logado):
    for caminho in ("templates/pessoas/cliente_form.html", "templates/frota/veiculo_form.html"):
        texto = (RAIZ / caminho).read_text()
        assert 'title="Técnico' not in texto
    html = usuario_logado.get(reverse("pessoas:novo")).content.decode()
    assert "ANTHROPIC_API_KEY" not in html


# ---------- histórico da Retaguarda ----------


def test_historico_em_portugues_e_com_valores_formatados(admin_client):
    carro = _carro("HIS1T00")
    carro.km_atual = 40_000
    carro.save()

    html = admin_client.get(
        reverse("admin:frota_veiculo_history", args=[carro.pk])
    ).content.decode()

    assert "Histórico: " in html or "Histórico de modificações" in html
    assert "Change reason" not in html and "Changes" not in html
    assert "Mudanças" in html
    assert "10.000" in html and "40.000" in html
    assert "10000" not in html


def test_historico_com_carro_apagado_nao_da_erro_500(admin_client):
    cliente = Cliente.objects.create(nome="Ana", cpf_cnpj="2")
    alocacao = _alocar(_carro("APA1G00"), cliente)
    # registro antigo apontando para um carro que não existe mais
    Alocacao.history.filter(id=alocacao.pk).update(veiculo_id=999_999)

    resposta = admin_client.get(reverse("admin:alocacoes_alocacao_history", args=[alocacao.pk]))

    assert resposta.status_code == 200


def test_historico_mostra_data_e_situacao_do_jeito_brasileiro():
    from config.historico import valor_em_portugues

    campo_data = Veiculo._meta.get_field("data_aquisicao")
    assert valor_em_portugues(campo_data, None) == "—"
    assert valor_em_portugues(campo_data, timezone.datetime(2026, 10, 6).date()) == "06/10/2026"
    campo_status = Veiculo._meta.get_field("status")
    assert valor_em_portugues(campo_status, "alocado") == "Alocado"


def test_historico_ligacao_vazia_ou_apagada_em_portugues():
    from simple_history.models import DeletedObject

    from config.historico import valor_em_portugues

    campo = Alocacao._meta.get_field("veiculo")
    assert valor_em_portugues(campo, DeletedObject(Veiculo, None)) == "—"
    assert valor_em_portugues(campo, DeletedObject(Veiculo, 7)) == "veículo nº 7 (apagado)"


# ---------- título da aba com a placa ----------


def test_titulo_da_aba_tem_a_placa_nas_telas_de_troca_e_devolucao():
    titulos = {
        "alocacoes/troca_nova.html": "Troca temporária — {{ alocacao.veiculo.placa }} — ",
        "alocacoes/troca_devolver.html": "Devolver substituto — {{ troca.veiculo_substituto.placa",
        "alocacoes/encerrar.html": "Devolução do carro — {{ alocacao.veiculo.placa }} — ",
        "km/lista.html": "KM do mês — Trade Rent a Car",
    }
    for arquivo, titulo in titulos.items():
        assert titulo in (RAIZ / "templates" / arquivo).read_text(), arquivo


# ---------- revisão adversarial (etapa 18b) ----------


def test_versao_antiga_com_carro_apagado_nao_da_erro_500(admin_client):
    # o caminho no topo chamava str() da versão antiga (lia veiculo.placa) → erro 500
    cliente = Cliente.objects.create(nome="Ana", cpf_cnpj="3")
    alocacao = _alocar(_carro("VER1S00"), cliente)
    Alocacao.history.filter(id=alocacao.pk).update(veiculo_id=999_999)
    versao = Alocacao.history.filter(id=alocacao.pk).first()

    resposta = admin_client.get(
        reverse("admin:alocacoes_alocacao_simple_history", args=[alocacao.pk, versao.pk])
    )

    assert resposta.status_code == 200
    html = resposta.content.decode()
    assert "Versão antiga" in html and f"nº {alocacao.pk}" in html


@pytest.mark.django_db
def test_clique_htmx_que_grava_sem_sessao_tambem_avisa(client):
    resposta = client.post(
        reverse("multas:nova"),
        {},
        HTTP_HX_REQUEST="true",
        HTTP_HX_CURRENT_URL="http://testserver/multas/",
    )
    assert resposta.status_code == 204
    assert resposta["HX-Redirect"] == reverse("login") + "?next=/multas/&expirou=1"

    sem_tela = client.post(reverse("multas:nova"), {}, HTTP_HX_REQUEST="true")
    assert sem_tela["HX-Redirect"] == reverse("login") + "?expirou=1"


@pytest.mark.django_db
def test_clique_htmx_so_de_leitura_sem_sessao_nao_fala_em_perder_dados(client):
    resposta = client.get(
        reverse("multas:lista"), HTTP_HX_REQUEST="true", HTTP_HX_CURRENT_URL="http://testserver/"
    )
    assert "expirou" not in resposta["HX-Redirect"]


@pytest.mark.django_db
def test_login_errado_na_tela_de_sessao_expirada_mostra_so_o_erro_de_senha(client):
    destino = client.post(reverse("multas:nova"))["Location"]
    html = client.post(destino, {"username": "x", "password": "y"}).content.decode()
    assert "Usuário ou senha incorretos" in html
    assert "o que você digitou NÃO foi salvo" not in html


@pytest.mark.django_db
def test_login_pela_retaguarda_tambem_renova_a_sessao(client, django_user_model):
    django_user_model.objects.create_superuser(username="dono", password="senha-forte-123")
    client.get(reverse("login"))  # carrega o sistema (como o servidor de verdade)
    client.post(reverse("admin:login"), {"username": "dono", "password": "senha-forte-123"})
    assert client.session["renovada_em"] == timezone.localdate().isoformat()


@pytest.mark.django_db
def test_renovar_a_sessao_empurra_a_validade_para_30_dias(client, django_user_model):
    from datetime import timedelta

    from django.contrib.sessions.models import Session

    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    client.post(reverse("login"), {"username": "dono", "password": "senha-forte-123"})
    sessao = client.session
    sessao["renovada_em"] = "2000-01-01"  # último uso foi outro dia
    sessao.save()
    # faltava 1 hora para a sessão vencer
    Session.objects.filter(session_key=sessao.session_key).update(
        expire_date=timezone.now() + timedelta(hours=1)
    )

    assert client.get(reverse("painel")).status_code == 200

    validade = Session.objects.get(session_key=sessao.session_key).expire_date
    assert validade > timezone.now() + timedelta(days=29)


@pytest.mark.django_db
def test_mesmo_dia_nao_grava_a_sessao_de_novo(client, django_user_model):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    client.post(reverse("login"), {"username": "dono", "password": "senha-forte-123"})
    client.get(reverse("buscar"))

    with CaptureQueriesContext(connection) as consultas:
        client.get(reverse("buscar"))

    gravou = [
        q for q in consultas.captured_queries if "UPDATE" in q["sql"] and "session" in q["sql"]
    ]
    assert not gravou


@pytest.mark.django_db
def test_formulario_de_verdade_aceita_data_sem_ano():
    from apps.multas.forms import MultaForm

    hoje = timezone.localdate()
    form = MultaForm(data={"data_infracao": hoje.strftime("%d/%m")})
    form.is_valid()
    assert "data_infracao" not in form.errors
    assert form.cleaned_data["data_infracao"] == hoje


def test_mensagem_de_data_nao_vaza_para_o_campo_da_classe():
    _FormData()
    _FormData()
    assert _FormData.base_fields["quando"].error_messages["invalid"] != MENSAGEM_DATA_INVALIDA
    form = _FormData(data={"quando": "xx"})
    assert form.errors["quando"] == [MENSAGEM_DATA_INVALIDA]
