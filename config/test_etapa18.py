"""Etapa 18, área global: painel, dinheiro, CSRF, logs, saúde do deploy e Admin."""

import json
import logging
import os
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import include, path, reverse

from apps import campos as modulo_campos
from apps.alocacoes.models import TrocaTemporaria
from apps.campos import TAMANHO_MAXIMO_UPLOAD, dinheiro_br
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente
from config.test_navegador_17 import _alocar, _carro

RAIZ = Path(__file__).resolve().parents[1]


# ---------- painel: ocupação ----------


def test_ocupacao_conta_carro_alugado_sem_troca(usuario_logado):
    """O filtro de troca aberta pegava toda alocação sem troca (LEFT JOIN): ocupação 0%."""
    for numero, placa in enumerate(["AAA1A11", "BBB2B22", "CCC3C33"]):
        cliente = Cliente.objects.create(nome=f"Cliente {numero}", cpf_cnpj=f"00{numero}")
        _alocar(_carro(placa), cliente)
    _carro("DDD4D44")

    resposta = usuario_logado.get(reverse("painel"))

    assert resposta.context["ocupacao"] == 75  # 3 alugados em 4 carros de locação


def test_ocupacao_com_e_sem_troca_ao_mesmo_tempo(usuario_logado):
    juliana = Cliente.objects.create(nome="Juliana", cpf_cnpj="001")
    marcos = Cliente.objects.create(nome="Marcos", cpf_cnpj="002")
    na_oficina = _carro("AAA1A11")
    alocacao = _alocar(na_oficina, juliana)
    _alocar(_carro("BBB2B22"), marcos)
    substituto = _carro("CCC3C33")
    _carro("DDD4D44")
    TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=alocacao.data_inicio,
        km_retirada=10_000,
    )
    assert Veiculo.objects.filter(status=Veiculo.Status.ALOCADO).count() == 3

    resposta = usuario_logado.get(reverse("painel"))

    # Marcos (sem troca) + Juliana (no substituto) = 2 de 4; o carro na oficina não conta
    assert resposta.context["ocupacao"] == 50


def test_card_de_atraso_usa_o_mesmo_nome_das_cobrancas(usuario_logado):
    pagina = usuario_logado.get(reverse("painel")).content.decode()
    assert "Em atraso" not in pagina
    assert "Atrasado" in pagina


# ---------- dinheiro digitado ----------


@pytest.mark.parametrize(
    ("digitado", "valor"),
    [("0,500", "0.50"), ("-0,250", "-0.25"), ("0.500", "0.50"), ("1.250,500", "1250.50")],
)
def test_zeros_a_mais_nos_centavos_nao_sao_ambiguos(digitado, valor):
    numero = dinheiro_br(digitado)
    assert numero == Decimal(valor)
    assert numero.as_tuple().exponent == -2  # passa no limite de 2 casas do campo


@pytest.mark.parametrize("digitado", ["12,000", "1,500", "-1,250"])
def test_virgula_com_tres_digitos_continua_ambigua(digitado):
    with pytest.raises(ValidationError):
        dinheiro_br(digitado)


def test_centavos_de_verdade_nao_sao_arredondados():
    # 0,505 tem meio centavo de verdade: fica como está e o campo acusa as 3 casas
    assert dinheiro_br("0,505") == Decimal("0.505")


# ---------- CSRF com cookies bloqueados ----------


def test_login_sem_cookie_explica_em_vez_de_voltar_ao_login_vazio(db):
    navegador = Client(enforce_csrf_checks=True)  # nunca recebeu o cookie

    resposta = navegador.post(reverse("login"), {"username": "x", "password": "y"})

    assert resposta.status_code == 403
    pagina = resposta.content.decode()
    assert "bloqueando os cookies" in pagina
    assert "Sua sessão expirou ou a página ficou aberta muito tempo" in pagina


def test_login_com_cookie_e_token_velho_continua_voltando_ao_login(db):
    navegador = Client(enforce_csrf_checks=True)
    navegador.get(reverse("login"))

    resposta = navegador.post(reverse("login"), {"username": "x", "password": "y"})

    assert resposta.status_code == 302
    assert resposta["Location"] == reverse("login")


def test_pagina_de_csrf_sem_aviso_de_cookie_quando_o_cookie_existe(django_user_model):
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    navegador = Client(enforce_csrf_checks=True)
    navegador.login(username="dono", password="senha-forte-123")
    navegador.get(reverse("painel"))

    resposta = navegador.post(reverse("logout"))

    assert resposta.status_code == 403
    assert "bloqueando os cookies" not in resposta.content.decode()


# ---------- produção: logs, saúde e banco ----------


def test_erros_do_django_vao_para_o_log_sem_depender_de_debug():
    handlers = logging.getLogger("django").handlers
    console = [h for h in handlers if isinstance(h, logging.StreamHandler)]
    assert console
    assert all(not h.filters for h in console)  # sem RequireDebugTrue
    assert logging.getLogger("django.request").getEffectiveLevel() <= logging.ERROR
    assert logging.getLogger("apps.financeiro.services").getEffectiveLevel() <= logging.INFO


def test_erro_500_e_registrado(db, caplog):
    from django.contrib.auth.models import User

    usuario = User.objects.create_user(username="dono", password="senha-forte-123")
    navegador = Client(raise_request_exception=False)
    navegador.force_login(usuario)
    with caplog.at_level(logging.ERROR, logger="django.request"):
        with override_settings(ROOT_URLCONF="config.test_etapa18"):
            resposta = navegador.get("/quebra/")
    assert resposta.status_code == 500
    assert any(r.name == "django.request" and r.exc_info for r in caplog.records)


def _quebra(request):
    raise RuntimeError("erro simulado")


# as rotas do sistema continuam valendo: a página 500 tem link para o Painel
urlpatterns = [path("quebra/", _quebra), path("", include("config.urls"))]


def test_saude_responde_ok_sem_login(client):
    resposta = client.get(reverse("saude"))
    assert resposta.status_code == 200
    assert resposta.content == b"ok"


@override_settings(
    SECURE_SSL_REDIRECT=True,
    SECURE_REDIRECT_EXEMPT=[r"^saude/$"],
    ALLOWED_HOSTS=["seuapp.up.railway.app", "healthcheck.railway.app"],
)
def test_sonda_do_railway_por_http_recebe_200(client):
    resposta = client.get("/saude/", HTTP_HOST="healthcheck.railway.app")
    assert resposta.status_code == 200
    # o resto do site continua indo para https
    assert client.get("/entrar/", HTTP_HOST="seuapp.up.railway.app").status_code == 301


def test_settings_de_producao_aceitam_a_sonda_e_reaproveitam_a_conexao():
    """Importa as settings como no Railway (DEBUG=False), num processo separado."""
    ambiente = {
        **os.environ,
        "DEBUG": "False",
        "SECRET_KEY": "chave-de-teste",
        "ALLOWED_HOSTS": "seuapp.up.railway.app",
        "DATABASE_URL": "sqlite:///:memory:",
    }
    codigo = (
        "import json, re; from config import settings as s; print(json.dumps({"
        "'hosts': s.ALLOWED_HOSTS, 'isento': any(re.search(p, 'saude/') for p in "
        "s.SECURE_REDIRECT_EXEMPT), 'conn': s.DATABASES['default']['CONN_MAX_AGE'], "
        "'debug': s.DEBUG}))"
    )
    saida = subprocess.run(
        [sys.executable, "-c", codigo],
        cwd=RAIZ,
        env=ambiente,
        capture_output=True,
        text=True,
        check=True,
    )
    dados = json.loads(saida.stdout.strip().splitlines()[-1])
    assert dados["debug"] is False
    assert dados["hosts"] == ["seuapp.up.railway.app", "healthcheck.railway.app"]
    assert dados["isento"] is True
    assert dados["conn"] == 60


def test_conexao_com_o_banco_e_reaproveitada():
    assert settings.DATABASES["default"]["CONN_HEALTH_CHECKS"] is True


def test_cron_do_railway_roda_a_rotina_e_nao_o_site():
    cron = json.loads((RAIZ / "railway.cron.json").read_text())["deploy"]
    assert cron["startCommand"] == "python manage.py rotina_diaria"
    assert cron["cronSchedule"] == "5 3 * * *"  # 00:05 em Brasília (UTC-3)
    web = json.loads((RAIZ / "railway.json").read_text())["deploy"]
    assert web["healthcheckPath"] == reverse("saude")
    assert "startCommand" not in web  # o web segue o Procfile
    exemplo = (RAIZ / ".env.example").read_text()
    for variavel in ("DEBUG=False", "PROXIES_CONFIAVEIS", "CNH_MODELO", "railway.cron.json"):
        assert variavel in exemplo


# ---------- Admin: limite de 10 MB nos arquivos ----------


def _arquivo_grande():
    return SimpleUploadedFile(
        "cnh.pdf", b"0" * (TAMANHO_MAXIMO_UPLOAD + 1), content_type="application/pdf"
    )


def test_admin_recusa_cnh_acima_de_10_mb(admin_client):
    resposta = admin_client.post(
        reverse("admin:pessoas_cliente_add"),
        {
            "nome": "Fulano",
            "cpf_cnpj": "52998224725",
            "status": Cliente.Status.ATIVO,
            "cnh_frente": _arquivo_grande(),
        },
    )
    assert resposta.status_code == 200  # voltou com erro, não gravou
    assert "Arquivo muito grande" in resposta.content.decode()
    assert not Cliente.objects.filter(nome="Fulano").exists()


def test_admin_limita_todo_campo_de_arquivo(rf, admin_user):
    import config.urls  # noqa: F401 — é onde o limite entra na Retaguarda
    from apps.campos import validar_tamanho_arquivo

    pedido = rf.get("/")
    pedido.user = admin_user
    for modelo, campos in (
        (Cliente, ("foto", "cnh_frente", "cnh_verso")),
        (Veiculo, ("foto", "documento")),
    ):
        formulario = admin.site._registry[modelo].get_form(pedido)
        for nome in campos:
            assert validar_tamanho_arquivo in formulario.base_fields[nome].validators


# ---------- revisão: data digitada sem ano ----------


@pytest.mark.parametrize(
    ("hoje", "digitado", "esperado"),
    [
        # recebimento de dezembro lançado no começo de janeiro: o dezembro que passou
        (date(2027, 1, 2), "28/12", date(2026, 12, 28)),
        # vencimento de janeiro combinado em dezembro: o janeiro que vem
        (date(2026, 12, 20), "05/01", date(2027, 1, 5)),
        (date(2026, 11, 10), "27/10", date(2026, 10, 27)),
        (date(2026, 11, 10), "10.11", date(2026, 11, 10)),
    ],
)
def test_data_sem_ano_fica_no_ano_mais_perto_de_hoje(monkeypatch, hoje, digitado, esperado):
    monkeypatch.setattr(modulo_campos.timezone, "localdate", lambda: hoje)
    assert modulo_campos.ler_data(digitado) == esperado


def test_29_de_fevereiro_sem_ano_em_ano_comum_e_recusado(monkeypatch):
    # não pula para 2028 (mais de um ano à frente): a pessoa confere a data
    monkeypatch.setattr(modulo_campos.timezone, "localdate", lambda: date(2027, 1, 2))
    with pytest.raises(ValidationError, match="inválida"):
        modulo_campos.ler_data("29/02")


# ---------- revisão: banco atrás do pooler do Neon ----------


def test_cursor_do_lado_do_servidor_desligado_por_causa_do_pooler():
    assert settings.DATABASES["default"]["DISABLE_SERVER_SIDE_CURSORS"] is True
