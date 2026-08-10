import pytest
from django.db import connection

from apps.multas.models import OrgaoAutuador


@pytest.fixture
def usuario_logado(client, django_user_model):
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    client.login(username="dono", password="senha-forte-123")
    return client


def dados(**extras):
    base = {
        "nome": "PBH",
        "esfera": "municipal",
        "portal": "https://portal.pbh.gov.br",
        "login": "trade-rent",
        "senha": "segredo-123",
        "email": "multas@pbh.gov.br",
        "telefone": "(31) 3277-0000",
        "procedimento": "Solicitar FICI e depois consultar",
        "endereco": "Av. Afonso Pena, 1212",
        "observacoes": "",
    }
    base.update(extras)
    return base


def senha_bruta(orgao):
    with connection.cursor() as cursor:
        cursor.execute("SELECT senha FROM multas_orgaoautuador WHERE id = %s", [orgao.pk])
        return cursor.fetchone()[0]


def test_telas_de_orgao_abrem(usuario_logado, db):
    orgao = OrgaoAutuador.objects.create(nome="DETRAN-MG", senha="abc")
    assert usuario_logado.get("/multas/orgaos/").status_code == 200
    assert usuario_logado.get("/multas/orgaos/novo/").status_code == 200
    assert usuario_logado.get(f"/multas/orgaos/{orgao.pk}/editar/").status_code == 200


def test_cadastrar_orgao_pela_tela_criptografa_a_senha(usuario_logado, db):
    resposta = usuario_logado.post("/multas/orgaos/novo/", dados())
    assert resposta.status_code == 302
    orgao = OrgaoAutuador.objects.get(nome="PBH")
    assert orgao.senha == "segredo-123"  # legível pela aplicação
    bruto = senha_bruta(orgao)
    assert bruto.startswith("fernet:")
    assert "segredo-123" not in bruto


def test_senha_nao_volta_no_html_e_vazia_mantem_a_atual(usuario_logado, db):
    """Revisão etapa 8: a senha não pode trafegar em claro no fonte da edição."""
    usuario_logado.post("/multas/orgaos/novo/", dados())
    orgao = OrgaoAutuador.objects.get(nome="PBH")

    resposta = usuario_logado.get(f"/multas/orgaos/{orgao.pk}/editar/")
    conteudo = resposta.content.decode()
    assert "segredo-123" not in conteudo  # nada da senha no HTML
    assert "no-store" in resposta.headers.get("Cache-Control", "")

    # reenviar com senha em branco mantém a atual
    usuario_logado.post(
        f"/multas/orgaos/{orgao.pk}/editar/", dados(telefone="(31) 99999-0000", senha="")
    )
    orgao.refresh_from_db()
    assert orgao.telefone == "(31) 99999-0000"
    assert orgao.senha == "segredo-123"
    assert senha_bruta(orgao).startswith("fernet:")


def test_editar_troca_a_senha(usuario_logado, db):
    usuario_logado.post("/multas/orgaos/novo/", dados())
    orgao = OrgaoAutuador.objects.get(nome="PBH")
    usuario_logado.post(f"/multas/orgaos/{orgao.pk}/editar/", dados(senha="nova-senha-456"))
    orgao.refresh_from_db()
    assert orgao.senha == "nova-senha-456"
    assert "nova-senha-456" not in senha_bruta(orgao)


def test_nome_duplicado_nao_cria_segundo_orgao(usuario_logado, db):
    usuario_logado.post("/multas/orgaos/novo/", dados())
    resposta = usuario_logado.post("/multas/orgaos/novo/", dados(login="outro"))
    assert resposta.status_code == 200
    assert "nome" in resposta.context["form"].errors
    assert OrgaoAutuador.objects.filter(nome="PBH").count() == 1


def test_lista_mostra_link_de_edicao(usuario_logado, db):
    orgao = OrgaoAutuador.objects.create(nome="DETRAN-MG")
    conteudo = usuario_logado.get("/multas/orgaos/").content.decode()
    assert f"/multas/orgaos/{orgao.pk}/editar/" in conteudo
    assert "/multas/orgaos/novo/" in conteudo
