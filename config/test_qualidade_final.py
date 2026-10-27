"""Qualidade final — global e painel: datas nos formulários, seletores, sessão
expirada com HTMX, páginas de erro, sair, painel e retaguarda."""

import re
from datetime import date, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from unittest import mock

import pytest
from django import forms
from django.contrib import admin
from django.test import Client, override_settings
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.campos import OPCAO_VAZIA, FormularioBR
from apps.financeiro.models import Cobranca
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente
from config import views

# ---------- formulário de volta: o que o navegador mandaria sem mexer em nada ----------


class _LeitorDeFormulario(HTMLParser):
    """Junta nome → valor dos campos do 1º <form method=post> como um navegador."""

    def __init__(self):
        super().__init__()
        self.dados, self._dentro, self._select, self._textarea = {}, False, None, None
        self.datas = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and a.get("method", "").lower() == "post":
            self._dentro = True
        if not self._dentro:
            return
        nome = a.get("name")
        if tag == "input" and nome:
            tipo = a.get("type", "text")
            if tipo == "file" or tipo in ("submit", "button"):
                return
            if tipo in ("checkbox", "radio") and "checked" not in a:
                return
            if tipo == "date":
                self.datas[nome] = a.get("value", "")
            self.dados[nome] = a.get("value", "on" if tipo == "checkbox" else "")
        elif tag == "select" and nome:
            self._select = nome
            self.dados.setdefault(nome, "")
        elif tag == "option" and self._select and "selected" in a:
            self.dados[self._select] = a.get("value", "")
        elif tag == "textarea" and nome:
            self._textarea = nome
            self.dados[nome] = ""

    def handle_endtag(self, tag):
        if tag == "form":
            self._dentro = False
        elif tag == "select":
            self._select = None
        elif tag == "textarea":
            self._textarea = None

    def handle_data(self, data):
        if self._textarea:
            self.dados[self._textarea] += data


def _ler(html):
    leitor = _LeitorDeFormulario()
    leitor.feed(html)
    return leitor


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(
        nome="Arlen Souza",
        cpf_cnpj="111.444.777-35",
        cnh_validade=date(2029, 5, 10),
        telefone="(31) 98888-7777",
    )


def test_edicao_do_cliente_mostra_a_validade_da_cnh_e_salvar_nao_apaga(usuario_logado, cliente):
    pagina = usuario_logado.get(f"/clientes/{cliente.pk}/editar/").content.decode()
    leitor = _ler(pagina)
    # antes vinha value="10/05/2029" num type=date: o navegador mostrava o campo vazio
    assert leitor.datas["cnh_validade"] == "2029-05-10"
    resposta = usuario_logado.post(f"/clientes/{cliente.pk}/editar/", leitor.dados)
    assert resposta.status_code == 302, resposta.content.decode()[:2000]
    cliente.refresh_from_db()
    assert cliente.cnh_validade == date(2029, 5, 10)


class _FormDeTeste(FormularioBR, forms.Form):
    data = forms.DateField(required=False)
    quando = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    escolha = forms.ChoiceField(choices=[("", "---------"), ("a", "A")], required=False)
    carro = forms.ModelChoiceField(queryset=Veiculo.objects.all(), required=False)
    personalizado = forms.ModelChoiceField(
        queryset=Veiculo.objects.all(), required=False, empty_label="Preencher sozinho pela data"
    )


@pytest.mark.django_db
def test_todo_campo_de_data_vira_calendario_com_valor_iso():
    form = _FormDeTeste(initial={"data": date(2029, 5, 10), "quando": date(2026, 1, 2)})
    html = str(form["data"]) + str(form["quando"])
    assert 'type="date"' in str(form["data"])
    assert 'value="2029-05-10"' in html
    assert 'value="2026-01-02"' in html


@pytest.mark.django_db
@pytest.mark.parametrize("digitado", ["0026-05-10", "1926-05-10", "2099-05-10"])
def test_ano_absurdo_e_recusado_com_mensagem_simples(digitado):
    form = _FormDeTeste(data={"data": digitado})
    assert not form.is_valid()
    assert "Confira o ano (ex.: 2026)." in form.errors["data"]


@pytest.mark.django_db
def test_ano_normal_e_aceito():
    form = _FormDeTeste(data={"data": "2026-10-27"})
    assert form.is_valid(), form.errors
    assert form.cleaned_data["data"] == date(2026, 10, 27)


@pytest.mark.django_db
def test_seletores_mostram_escolha_no_lugar_dos_tracinhos():
    form = _FormDeTeste()
    assert form.fields["escolha"].choices[0] == ("", OPCAO_VAZIA)
    assert form.fields["carro"].empty_label == OPCAO_VAZIA
    # rótulo vazio escolhido de propósito fica como está
    assert form.fields["personalizado"].empty_label == "Preencher sozinho pela data"
    assert "---------" not in str(form)


@pytest.mark.django_db
def test_cadastro_do_cliente_nao_mostra_tracinhos(usuario_logado):
    pagina = usuario_logado.get("/clientes/novo/").content.decode()
    assert "---------" not in pagina


# ---------- painel: rotina parada falsa depois de mudar o dia de vencimento ----------


def _alocacao(cliente, data_inicio, dia):
    veiculo = Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_atual=50_000)
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=data_inicio,
        dia_vencimento=dia,
        valor_semanal=Decimal("700.00"),
        km_entrega=50_000,
    )


def _aluguel(cliente, alocacao, vencimento, inicio=None, fim=None, **extra):
    return Cobranca.objects.create(
        cliente=cliente,
        alocacao=alocacao,
        origem=Cobranca.Origem.ALUGUEL,
        descricao="Aluguel",
        valor=Decimal("700.00"),
        vencimento=vencimento,
        periodo_inicio=inicio,
        periodo_fim=fim,
        **extra,
    )


def test_mudar_dia_de_vencimento_nao_acusa_rotina_parada(cliente):
    # pagava às sextas; a semana de 16/10 cobre 16 a 22/10. Na quarta 21/10 o
    # dia passou para segunda: o 1º dia novo é 19/10 (já passou), mas ele só
    # cobra a partir de 23/10 — nada de uso está sem cobrança
    alocacao = _alocacao(cliente, date(2026, 9, 4), dia=4)
    _aluguel(cliente, alocacao, date(2026, 10, 16), date(2026, 10, 16), date(2026, 10, 22))
    alocacao.dia_vencimento = 0
    alocacao.save()
    _, nao_lancadas = views._alugueis_da_semana(
        (date(2026, 10, 19), date(2026, 10, 25)), date(2026, 10, 21)
    )
    assert nao_lancadas == 0


def test_semana_realmente_esquecida_continua_acusando_rotina_parada(cliente):
    alocacao = _alocacao(cliente, date(2026, 9, 4), dia=4)
    _aluguel(cliente, alocacao, date(2026, 10, 9), date(2026, 10, 9), date(2026, 10, 15))
    # 16/10 venceu e não foi lançada; hoje é 21/10
    _, nao_lancadas = views._alugueis_da_semana(
        (date(2026, 10, 19), date(2026, 10, 25)), date(2026, 10, 21)
    )
    assert nao_lancadas == 1


def test_previsto_da_transicao_cobra_so_os_dias_nao_pagos(cliente):
    # mesmo cenário da mudança de dia: 19/10 cobra 23 a 25/10 (3 dias de 700/7)
    alocacao = _alocacao(cliente, date(2026, 9, 4), dia=4)
    _aluguel(cliente, alocacao, date(2026, 10, 16), date(2026, 10, 16), date(2026, 10, 22))
    alocacao.dia_vencimento = 0
    alocacao.save()
    previsto, _ = views._alugueis_da_semana(
        (date(2026, 10, 19), date(2026, 10, 25)), date(2026, 10, 21)
    )
    assert previsto == Decimal("300.00")


# ---------- painel: textos e links ----------


def test_card_em_atraso_inclui_a_cobranca_judicial(usuario_logado, cliente):
    hoje = timezone.localdate()
    _aluguel(cliente, None, hoje - timedelta(days=30), status=Cobranca.Status.JUDICIAL)
    _aluguel(cliente, None, hoje - timedelta(days=3), status=Cobranca.Status.ATRASADO)
    resposta = usuario_logado.get("/")
    assert resposta.context["atrasado_total"] == Decimal("1400.00")
    assert resposta.context["atrasado_judicial"] == Decimal("700.00")
    assert "inclui R$ 700,00 na Justiça" in resposta.content.decode()


def test_painel_sem_textos_tecnicos(usuario_logado):
    with mock.patch("config.views.rotina_atrasada", return_value=True):
        pagina = usuario_logado.get("/").content.decode()
    assert "rotina_diaria" not in pagina
    assert "atualização automática da noite" in pagina


def test_painel_le_a_data_do_fuso_do_sistema(usuario_logado):
    with mock.patch("config.views.timezone.localdate", return_value=date(2026, 10, 19)):
        resposta = usuario_logado.get("/")
    assert resposta.context["hoje"] == date(2026, 10, 19)


def test_textos_do_painel_no_template():
    from pathlib import Path

    texto = (Path(__file__).resolve().parents[1] / "templates" / "painel.html").read_text()
    assert "Candidatos à desmobilização" not in texto
    assert "Carros para pensar em vender" in texto
    assert "ver quando vender" in texto
    assert "p.texto_km" in texto  # "vence agora" / "vencida há ..." (retomada 17)
    assert "{% url 'multas:editar' multa.pk %}" in texto
    assert "{% url 'sinistros:editar' sinistro.pk %}" in texto


# ---------- sessão expirada em clique HTMX ----------


@pytest.mark.django_db
def test_htmx_sem_login_vai_para_o_login_em_vez_de_encaixar_a_pagina():
    resposta = Client().post(
        "/km/registrar/1/",
        HTTP_HX_REQUEST="true",
        HTTP_HX_CURRENT_URL="http://testserver/km/?mes=2026-10",
    )
    assert resposta.status_code == 204
    assert resposta["HX-Redirect"] == "/entrar/?next=/km/%3Fmes%3D2026-10"


@pytest.mark.django_db
def test_htmx_com_url_de_outro_site_nao_vira_next():
    resposta = Client().get(
        "/", HTTP_HX_REQUEST="true", HTTP_HX_CURRENT_URL="https://golpe.example/x"
    )
    assert resposta.status_code == 204
    assert resposta["HX-Redirect"] == "/entrar/"


@pytest.mark.django_db
def test_sem_htmx_o_redirect_continua_normal():
    resposta = Client().get("/km/")
    assert resposta.status_code == 302
    assert resposta["Location"].startswith("/entrar/")


# ---------- páginas de erro, login e sair ----------


def test_404_em_portugues_com_link_para_o_painel(usuario_logado):
    with override_settings(DEBUG=False):
        resposta = usuario_logado.get("/nao-existe-mesmo/")
    assert resposta.status_code == 404
    pagina = resposta.content.decode()
    assert "Página não encontrada" in pagina
    assert 'href="/"' in pagina


def test_500_renderiza_sem_contexto():
    from django.template import loader

    html = loader.get_template("500.html").render()
    assert "Algo deu errado" in html


@pytest.mark.django_db
def test_403_csrf_explica_sessao_expirada(django_user_model):
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    navegador = Client(enforce_csrf_checks=True)
    navegador.login(username="dono", password="senha-forte-123")
    resposta = navegador.post("/financeiro/receber/", {})
    assert resposta.status_code == 403
    pagina = resposta.content.decode()
    assert "Sua sessão expirou ou a página ficou aberta muito tempo" in pagina
    assert "entrar de novo" in pagina


def test_login_aberto_ja_logado_vai_ao_painel(usuario_logado):
    resposta = usuario_logado.get("/entrar/")
    assert resposta.status_code == 302
    assert resposta["Location"] == "/"


def test_get_em_sair_pergunta_em_vez_de_pagina_em_branco(usuario_logado):
    resposta = usuario_logado.get("/sair/")
    assert resposta.status_code == 200
    assert "Deseja sair" in resposta.content.decode()
    # e continua logado: GET não desloga
    assert usuario_logado.get("/").status_code == 200


def test_post_em_sair_desloga(usuario_logado):
    resposta = usuario_logado.post("/sair/")
    assert resposta.status_code == 302
    assert usuario_logado.get("/").status_code == 302


@pytest.mark.django_db
def test_sair_sem_login_vai_ao_login():
    resposta = Client().get("/sair/")
    assert resposta.status_code == 302
    assert resposta["Location"].startswith("/entrar/")


@pytest.mark.django_db
def test_login_com_next_sair_vai_ao_painel(django_user_model):
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    resposta = Client().post(
        "/entrar/?next=/sair/", {"username": "dono", "password": "senha-forte-123"}
    )
    assert resposta.status_code == 302
    assert resposta["Location"] == "/"


@pytest.mark.django_db
def test_login_com_next_normal_continua_funcionando(django_user_model):
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    resposta = Client().post(
        "/entrar/?next=/km/", {"username": "dono", "password": "senha-forte-123"}
    )
    assert resposta["Location"] == "/km/"


# ---------- retaguarda ----------


def test_admin_com_a_marca_e_sem_logs_do_axes_em_ingles(admin_client):
    from axes.models import AccessAttempt, AccessFailureLog, AccessLog

    assert admin.site.site_header == "Trade Rent a Car — Retaguarda"
    assert admin.site.is_registered(AccessAttempt)
    assert not admin.site.is_registered(AccessLog)
    assert not admin.site.is_registered(AccessFailureLog)
    pagina = admin_client.get("/admin/").content.decode()
    assert "Trade Rent a Car — Retaguarda" in pagina
    assert "Access logs" not in pagina
    assert re.search(r"Bloqueios de login", pagina)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("caminho", "campos"),
    [
        ("apps.multas.forms.MultaEdicaoForm", ["data_infracao", "fici_prazo"]),
        ("apps.manutencao.views.ManutencaoForm", ["data", "data_entrada", "data_saida"]),
        ("apps.manutencao.views.VoltouDaOficinaForm", ["data_saida"]),
        ("apps.pessoas.views.ClienteForm", ["cnh_validade"]),
    ],
)
def test_formularios_de_edicao_mostram_a_data_salva(caminho, campos):
    """Datas gravadas voltam no formato que o calendário do navegador entende."""
    from importlib import import_module

    modulo, nome = caminho.rsplit(".", 1)
    classe = getattr(import_module(modulo), nome)
    form = classe(initial={campo: date(2029, 5, 10) for campo in campos})
    for campo in campos:
        html = str(form[campo])
        assert 'type="date"' in html, campo
        assert 'value="2029-05-10"' in html, (campo, html)
