from django.db import connection
from django.test import override_settings

from apps.multas.models import OrgaoAutuador


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


def test_lista_nunca_manda_a_senha_no_html(usuario_logado, db):
    """A senha não pode ir no fonte da listagem — nem escondida por trás de um clique."""
    OrgaoAutuador.objects.create(nome="PBH", login="trade-rent", senha="segredo-123")
    resposta = usuario_logado.get("/multas/orgaos/")
    conteudo = resposta.content.decode()
    assert "segredo-123" not in conteudo
    assert "trade-rent" in conteudo  # o usuário do portal continua visível
    assert "no-store" in resposta.headers.get("Cache-Control", "")


def test_admin_nao_expoe_a_senha_nem_guarda_no_historico(client, django_user_model, db):
    """O /admin usa o mesmo form das telas: senha fora do HTML e fora do histórico."""
    django_user_model.objects.create_superuser(username="chefe", password="senha-forte-123")
    client.login(username="chefe", password="senha-forte-123")
    orgao = OrgaoAutuador.objects.create(nome="PBH", login="trade-rent", senha="segredo-123")
    orgao.senha = "segredo-456"
    orgao.save()

    conteudo = client.get(f"/admin/multas/orgaoautuador/{orgao.pk}/change/").content.decode()
    assert "segredo-123" not in conteudo and "segredo-456" not in conteudo

    # nenhuma versão histórica guarda credenciais
    historico = orgao.history.all()
    assert historico.count() == 2
    for versao in historico:
        assert not hasattr(versao, "senha") and not hasattr(versao, "login")
        tela = client.get(_url_historico(orgao, versao))
        assert tela.status_code == 200
        assert "segredo" not in tela.content.decode()

    # salvar pelo admin com a senha em branco mantém a atual
    resposta = client.post(
        f"/admin/multas/orgaoautuador/{orgao.pk}/change/",
        dados(nome="PBH", senha="", telefone="(31) 3333-0000"),
    )
    assert resposta.status_code == 302
    orgao.refresh_from_db()
    assert orgao.senha == "segredo-456"


def _url_historico(orgao, versao):
    return f"/admin/multas/orgaoautuador/{orgao.pk}/history/{versao.history_id}/"


def test_trocar_a_secret_key_nao_afeta_as_credenciais(db):
    """A criptografia usa CREDENCIAIS_KEY — a SECRET_KEY pode ser rotacionada."""
    orgao = OrgaoAutuador.objects.create(nome="DETRAN-MG", senha="segredo-123")
    with override_settings(SECRET_KEY="outra-chave-completamente-diferente"):
        assert OrgaoAutuador.objects.get(pk=orgao.pk).senha == "segredo-123"


def test_credencial_ilegivel_volta_vazia_e_avisa_no_log(db, caplog):
    """Chave errada não pode devolver o texto cifrado disfarçado de senha."""
    orgao = OrgaoAutuador.objects.create(nome="BHTrans", senha="segredo-123")
    with override_settings(CREDENCIAIS_KEY="chave-que-nao-foi-a-da-gravacao"):
        recarregado = OrgaoAutuador.objects.get(pk=orgao.pk)
        assert recarregado.senha == ""
    assert "Credencial ilegível" in caplog.text
    assert "multas.OrgaoAutuador.senha" in caplog.text
