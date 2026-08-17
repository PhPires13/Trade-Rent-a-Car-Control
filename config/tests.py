from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, override_settings


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


def test_secret_key_e_obrigatoria_em_producao():
    """Sem a variável no Railway o app não pode subir com a chave pública do repo."""
    from config.settings import CHAVE_DE_DESENVOLVIMENTO, chave_secreta

    with pytest.raises(ImproperlyConfigured):
        chave_secreta("", debug=False)
    assert chave_secreta("chave-de-verdade", debug=False) == "chave-de-verdade"
    assert chave_secreta("", debug=True) == CHAVE_DE_DESENVOLVIMENTO  # dev segue funcionando


@pytest.mark.django_db
@override_settings(AXES_ENABLED=True)
def test_login_normal_continua_funcionando_com_o_bloqueio_ligado(django_user_model):
    """O backend do axes não pode atrapalhar quem digita a senha certa."""
    from axes.utils import reset

    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    cliente_http = Client()
    try:
        cliente_http.post("/entrar/", {"username": "dono", "password": "errada"})
        resposta = cliente_http.post(
            "/entrar/", {"username": "dono", "password": "senha-forte-123"}
        )
        assert resposta.status_code == 302
        assert cliente_http.get("/").status_code == 200
    finally:
        reset()


@pytest.mark.django_db
@override_settings(AXES_ENABLED=True)
def test_login_bloqueia_apos_tentativas_erradas(django_user_model, settings):
    """Força bruta no /entrar/ trava a conta por 15 minutos (django-axes)."""
    from axes.utils import reset

    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    cliente_http = Client()
    credenciais = {"username": "dono", "password": "chute-errado"}
    try:
        for _ in range(settings.AXES_FAILURE_LIMIT - 1):
            assert cliente_http.post("/entrar/", credenciais).status_code == 200
        bloqueado = cliente_http.post("/entrar/", credenciais)
        assert bloqueado.status_code == 429  # too many requests
        assert "bloqueado por 15 minutos" in bloqueado.content.decode()
        # a senha certa também não passa enquanto durar o bloqueio
        travado = cliente_http.post("/entrar/", {"username": "dono", "password": "senha-forte-123"})
        assert travado.status_code == 429
    finally:
        reset()


@pytest.mark.django_db
def test_painel_nao_cresce_com_a_frota(django_user_model, django_assert_max_num_queries):
    """Revisão de performance: o painel montava a ficha financeira carro a carro.

    Com 12 veículos eram 241 queries (10 por carro só no bloco de candidatos à
    venda, mais 6 por carro nas preventivas). Os blocos passaram a ser em lote —
    o teto aqui não pode voltar a acompanhar o tamanho da frota.
    """
    from apps.frota.models import Veiculo
    from apps.km.models import RegistroKm
    from apps.manutencao.models import ItemPreventiva, Manutencao
    from apps.sinistros.models import Sinistro

    hoje = date.today()
    oleo = ItemPreventiva.objects.get(nome="Troca de óleo e filtro")
    for indice in range(12):
        veiculo = Veiculo.objects.create(
            placa=f"TQ{indice:02d}A{indice:02d}",
            marca_modelo="Gol",
            km_atual=90_000,
            km_compra=60_000,
            data_aquisicao=date(2024, 7, 1),
            valor_compra=Decimal("42000.00"),
            mensalidade_protecao=Decimal("304.00"),
        )
        Manutencao.objects.create(  # preventiva vencida: entra no bloco de alertas
            veiculo=veiculo,
            item=oleo,
            tipo="preventiva",
            data=hoje - timedelta(days=30),
            km=80_000,
            descricao="Óleo",
            custo_real=Decimal("300.00"),
        )
        RegistroKm.objects.create(
            veiculo=veiculo,
            mes_referencia=hoje.replace(day=1),
            data_leitura=hoje,
            km=90_000,
        )
        Sinistro.objects.create(  # bloco de sinistros abertos, com dias parado por sinistro
            veiculo=veiculo,
            data=hoje - timedelta(days=40),
            envolvido="terceiro",
            franquia_valor=Decimal("1500.00"),
        )

    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    cliente_http = Client()
    cliente_http.login(username="dono", password="senha-forte-123")
    with django_assert_max_num_queries(45):
        resposta = cliente_http.get("/")
    assert resposta.status_code == 200
    conteudo = resposta.content.decode()
    assert "TQ00A00" in conteudo  # alertas de preventiva continuam na tela


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
