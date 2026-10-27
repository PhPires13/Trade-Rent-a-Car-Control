"""Pendências das etapas 14/15 na frente alocações: voltar à ficha, troca, oficina, Recebi."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.contrib.admin.sites import site
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory

from apps.alocacoes.models import Alocacao, TrocaTemporaria, Vistoria
from apps.financeiro import services as financeiro
from apps.financeiro.models import Cobranca
from apps.frota.models import Veiculo
from apps.manutencao.models import Manutencao
from apps.pessoas.models import Cliente

HOJE = date.today()


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="PND1A11", marca_modelo="Onix", km_atual=40_000)


@pytest.fixture
def substituto(db):
    return Veiculo.objects.create(placa="PND2B22", marca_modelo="HB20", km_atual=30_000)


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Carla", cpf_cnpj="333.444.555-66", telefone="11988887777")


@pytest.fixture
def alocacao(veiculo, cliente):
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=3),
        valor_semanal=Decimal("700.00"),
        km_entrega=40_000,
    )


def _na_oficina(veiculo):
    return Manutencao.objects.create(
        veiculo=veiculo,
        tipo=Manutencao.Tipo.CORRETIVA,
        data=HOJE - timedelta(days=1),
        data_entrada=HOJE - timedelta(days=1),
    )


# ---------- carro ainda na oficina não volta como Disponível ----------


def test_encerrar_com_carro_na_oficina_fica_em_manutencao(alocacao, veiculo):
    _na_oficina(veiculo)
    alocacao.encerrar(HOJE, km_devolucao=40_500)
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.EM_MANUTENCAO


def test_encerrar_sem_oficina_fica_disponivel(alocacao, veiculo):
    alocacao.encerrar(HOJE, km_devolucao=40_500)
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.DISPONIVEL


def test_devolver_substituto_que_esta_na_oficina_fica_em_manutencao(alocacao, substituto):
    troca = TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=HOJE - timedelta(days=1),
        km_retirada=30_000,
    )
    _na_oficina(substituto)
    troca.devolver(HOJE, km_devolucao=30_200)
    substituto.refresh_from_db()
    assert substituto.status == Veiculo.Status.EM_MANUTENCAO


# ---------- troca: oficina na escolha, valor atual e novo valor da semana ----------


def test_troca_nova_marca_substituto_na_oficina_e_mostra_valor_atual(
    usuario_logado, alocacao, substituto
):
    _na_oficina(substituto)
    html = usuario_logado.get(f"/alocacoes/{alocacao.pk}/troca/").content.decode()
    assert "PND2B22 — HB20 ⚠ na oficina desde" in html
    assert "Hoje a semana custa R$ 700,00" in html


def test_troca_nova_avisa_oficina_e_mostra_novo_valor_da_semana(
    usuario_logado, alocacao, substituto
):
    financeiro.gerar_cobrancas_semanais()
    semana = alocacao.cobrancas.get(origem=Cobranca.Origem.ALUGUEL)
    _na_oficina(substituto)
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/troca/",
        {
            "veiculo_substituto": substituto.pk,
            "data_retirada": HOJE.isoformat(),
            "km_retirada": "30000",
            "valor_semanal_ajustado": "840,00",
        },
        follow=True,
    )
    textos = [str(m) for m in resposta.context["messages"]]
    semana.refresh_from_db()
    assert semana.valor != Decimal("700.00")
    assert any("consta como na oficina" in t for t in textos)
    assert any(
        f"vencimento em {semana.vencimento:%d/%m/%Y} passou a" in t and "R$" in t for t in textos
    )


def test_tirar_valor_ajustado_no_admin_desfaz_o_rateio(alocacao, substituto, admin_user):
    financeiro.gerar_cobrancas_semanais()
    semana = alocacao.cobrancas.get(origem=Cobranca.Origem.ALUGUEL)
    troca = TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=HOJE,
        km_retirada=30_000,
        valor_semanal_ajustado=Decimal("840.00"),
    )
    semana.refresh_from_db()
    assert semana.valor != Decimal("700.00")
    # Admin: só valores mudam — tirar o ajuste volta a semana ao preço do contrato
    troca.valor_semanal_ajustado = None
    request = RequestFactory().post("/")
    request.user = admin_user
    request.session = {}
    request._messages = FallbackStorage(request)
    site._registry[TrocaTemporaria].save_model(request, troca, None, True)
    semana.refresh_from_db()
    assert semana.valor == Decimal("700.00")
    assert any("passou a R$ 700,00" in str(m) for m in request._messages)


# ---------- ?voltar= leva de volta à ficha ----------


def test_encerrar_vindo_da_ficha_volta_para_ela(usuario_logado, alocacao, cliente):
    ficha = f"/clientes/{cliente.pk}/"
    url = f"/alocacoes/{alocacao.pk}/encerrar/?voltar={ficha}"
    assert f'href="{ficha}"' in usuario_logado.get(url).content.decode()
    resposta = usuario_logado.post(url, {"data_termino": HOJE.isoformat(), "km_devolucao": "40100"})
    assert resposta.status_code == 302 and resposta.url == ficha


def test_voltar_de_outro_site_e_ignorado(usuario_logado, alocacao):
    html = usuario_logado.get(
        f"/alocacoes/{alocacao.pk}/vistoria/nova/?voltar=https://golpe.example.com/"
    ).content.decode()
    assert "golpe.example.com" not in html
    assert 'href="/alocacoes/"' in html


def test_cancelar_da_vistoria_e_da_troca_volta_para_a_ficha(usuario_logado, alocacao, cliente):
    ficha = f"/clientes/{cliente.pk}/"
    for url in (
        f"/alocacoes/{alocacao.pk}/vistoria/nova/?voltar={ficha}",
        f"/alocacoes/{alocacao.pk}/troca/?voltar={ficha}",
        f"/alocacoes/{alocacao.pk}/editar/?voltar={ficha}",
    ):
        assert f'href="{ficha}"' in usuario_logado.get(url).content.decode(), url


# ---------- vistoria: tipo escolhido sozinho e KM de dica ----------


def test_vistoria_sem_tipo_comeca_pela_entrega_e_depois_devolucao(usuario_logado, alocacao):
    url = f"/alocacoes/{alocacao.pk}/vistoria/nova/"
    html = usuario_logado.get(url).content.decode()
    assert '<option value="entrada" selected>' in html
    assert "último conhecido: 40.000 km" in html
    Vistoria.objects.create(alocacao=alocacao, tipo=Vistoria.Tipo.ENTRADA, data=HOJE, km=40_000)
    html = usuario_logado.get(url).content.decode()
    assert '<option value="saida" selected>' in html


# ---------- lista de alocações: deve + Recebi ----------


def test_lista_mostra_divida_e_recebi_so_para_quem_deve(usuario_logado, alocacao, cliente):
    html = usuario_logado.get("/alocacoes/").content.decode()
    assert f"/financeiro/receber/?cliente={cliente.pk}" not in html
    Cobranca.objects.create(
        cliente=cliente,
        alocacao=alocacao,
        descricao="Semana",
        valor=Decimal("700.00"),
        vencimento=HOJE - timedelta(days=2),
    )
    html = usuario_logado.get("/alocacoes/").content.decode()
    assert html.count(f"/financeiro/receber/?cliente={cliente.pk}") == 2  # cartão + tabela
    assert "R$ 700,00 vencido" in html  # mesma conta do Quem deve (etapa final)


# ---------- revisão: vistoria de devolução não perde o ?voltar= ----------


def test_encerrar_com_voltar_leva_o_voltar_para_a_vistoria(usuario_logado, alocacao, cliente):
    from urllib.parse import parse_qs, urlparse

    ficha = f"/clientes/{cliente.pk}/"
    url = f"/alocacoes/{alocacao.pk}/encerrar/?voltar={ficha}"
    resposta = usuario_logado.get(url)
    link = resposta.context["url_vistoria_devolucao"]
    query = parse_qs(urlparse(link).query)
    assert query["tipo"] == ["saida"]
    # a vistoria volta ao encerrar, que por sua vez ainda sabe voltar à ficha
    assert query["voltar"] == [url]


def test_dois_cartoes_de_passos_nao_repetem_o_id(usuario_logado, cliente):
    for placa in ("PND3C33", "PND4D44"):
        Alocacao.objects.create(
            veiculo=Veiculo.objects.create(placa=placa, marca_modelo="Gol", km_atual=1_000),
            cliente=cliente,
            data_inicio=HOJE,
            valor_semanal=Decimal("600.00"),
            km_entrega=1_000,
        )
    html = usuario_logado.get(f"/clientes/{cliente.pk}/").content.decode()
    assert html.count('id="passos-entrega-titulo"') == 0
    assert html.count('id="passos-entrega-titulo-') in (0, 2)
