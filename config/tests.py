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
