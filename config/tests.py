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
    """Tailwind/HTMX/Alpine/Chart.js servidos localmente — funciona sem internet."""
    from django.contrib.staticfiles import finders

    for asset in ["tailwind.js", "htmx.min.js", "alpine.min.js", "chart.umd.js"]:
        assert finders.find(f"vendor/{asset}"), f"vendor/{asset} não encontrado"
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    cliente_http = Client()
    cliente_http.login(username="dono", password="senha-forte-123")
    conteudo = cliente_http.get("/").content.decode()
    assert "cdn." not in conteudo and "unpkg" not in conteudo
    assert "vendor/tailwind.js" in conteudo


def _cobrancas_no_painel(quantidade):
    """`quantidade` cobranças da semana com R$ 100 pagos e outras tantas em atraso."""
    from apps.financeiro.models import AplicacaoRecebimento, Cobranca, Recebimento
    from apps.pessoas.models import Cliente

    hoje = date.today()
    cliente = Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")
    recebimento = Recebimento.objects.create(cliente=cliente, data=hoje, valor=Decimal("9999"))
    for _ in range(quantidade):
        da_semana = Cobranca.objects.create(
            cliente=cliente,
            origem=Cobranca.Origem.ALUGUEL,
            descricao="Aluguel",
            valor=Decimal("650.00"),
            vencimento=hoje,
        )
        AplicacaoRecebimento.objects.create(
            recebimento=recebimento, cobranca=da_semana, valor=Decimal("100.00")
        )
        Cobranca.objects.create(
            cliente=cliente,
            origem=Cobranca.Origem.ALUGUEL,
            descricao="Aluguel antigo",
            valor=Decimal("650.00"),
            vencimento=hoje - timedelta(days=30),
            status=Cobranca.Status.ATRASADO,
        )


@pytest.mark.django_db
def test_painel_nao_cresce_com_as_cobrancas(usuario_logado, django_assert_max_num_queries):
    """Recebido da semana e saldo em atraso somavam 2 queries por cobrança.

    Com 10 cobranças da semana e 10 atrasadas eram 65 queries; agora o total
    quitado vem anotado em lote (Cobranca.com_quitacao_anotada).
    """
    _cobrancas_no_painel(10)
    with django_assert_max_num_queries(20):
        resposta = usuario_logado.get("/")
    contexto = resposta.context
    assert contexto["semana_total"] == Decimal("6500.00")
    assert contexto["semana_recebido"] == Decimal("1000.00")
    assert contexto["percentual_semana"] == 15
    assert contexto["atrasado_total"] == Decimal("6500.00")
    assert "R$ 1.000,00" in resposta.content.decode()


@pytest.mark.django_db
def test_painel_conta_a_frota_por_status_e_a_ocupacao(usuario_logado):
    """Contagens do bloco Frota numa query só — mesmos números de antes."""
    from apps.frota.models import Veiculo

    Status, Uso = Veiculo.Status, Veiculo.Uso
    for indice, (status, uso) in enumerate(
        [
            (Status.ALOCADO, Uso.LOCACAO),
            (Status.ALOCADO, Uso.LOCACAO),
            (Status.DISPONIVEL, Uso.LOCACAO),
            (Status.EM_MANUTENCAO, Uso.LOCACAO),
            (Status.DISPONIVEL, Uso.FORA_LOCACAO),  # fora da ocupação, mas na frota
            (Status.VENDIDO, Uso.LOCACAO),  # vendido some do painel
        ]
    ):
        Veiculo.objects.create(placa=f"TQC{indice}A00", marca_modelo="Gol", status=status, uso=uso)
    contexto = usuario_logado.get("/").context
    assert contexto["total_veiculos"] == 5
    assert contexto["veiculos_por_status"] == [
        ("disponivel", "Disponível", 2),
        ("alocado", "Alocado", 2),
        ("em_manutencao", "Em manutenção", 1),
        ("inativo", "Inativo", 0),
    ]
    assert contexto["ocupacao"] == 50  # 2 alugados de 4 carros de locação


@pytest.mark.django_db
def test_painel_sem_frota_nao_divide_por_zero(usuario_logado):
    contexto = usuario_logado.get("/").context
    assert contexto["total_veiculos"] == 0
    assert contexto["ocupacao"] is None
    assert contexto["percentual_semana"] == 0


@pytest.mark.django_db
def test_busca_so_com_hifen_nao_devolve_a_frota_inteira(usuario_logado):
    """normalizar_placa("-") é "" — e placa__icontains="" casava com todo carro."""
    from apps.frota.models import Veiculo

    Veiculo.objects.create(placa="ABC1D23", marca_modelo="Gol")
    resposta = usuario_logado.get("/buscar/", {"q": "-"})
    assert resposta.status_code == 200  # antes: redirecionava para o único carro
    assert resposta.context["veiculos"] == []
    # a busca por placa com hífen continua achando
    assert usuario_logado.get("/buscar/", {"q": "abc-1d23"}).status_code == 302
