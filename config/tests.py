import pytest
from django.test import Client


@pytest.mark.django_db
def test_painel_exige_login():
    resposta = Client().get("/")
    assert resposta.status_code == 302
    assert resposta.url.startswith("/entrar/")


@pytest.mark.django_db
def test_login_e_painel(django_user_model):
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    cliente_http = Client()
    assert cliente_http.login(username="dono", password="senha-forte-123")
    resposta = cliente_http.get("/")
    assert resposta.status_code == 200
    assert "Painel" in resposta.content.decode()


@pytest.mark.django_db
def test_assets_locais_sem_cdn(django_user_model):
    """Etapa 9: Tailwind/HTMX/Alpine servidos localmente — funciona sem internet."""
    from django.contrib.staticfiles import finders

    for asset in ["tailwind.js", "htmx.min.js", "alpine.min.js", "chart.umd.js"]:
        assert finders.find(f"vendor/{asset}"), f"vendor/{asset} não encontrado"
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    cliente_http = Client()
    cliente_http.login(username="dono", password="senha-forte-123")
    conteudo = cliente_http.get("/").content.decode()
    assert "cdn." not in conteudo and "unpkg" not in conteudo
    assert "vendor/tailwind.js" in conteudo
