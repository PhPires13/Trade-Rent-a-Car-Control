"""Etapa 18b — revisão das correções do navegador na área de KM.

Mês esquecido cujo excedente falha (o aviso "era X, agora é Y" não pode sair
se tudo foi desfeito), correção da 1ª leitura de carro com KM de compra sem data
e o KM atual subido à mão na ficha, que a "Última" do mês atual escondia.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.financeiro.models import Cobranca
from apps.frota.models import Veiculo
from apps.km.correcao import suspeita_do_km
from apps.km.models import RegistroKm
from apps.pessoas.models import Cliente


def _leitura(veiculo, data, km):
    registro = RegistroKm(
        veiculo=veiculo, mes_referencia=data.replace(day=1), data_leitura=data, km=km
    )
    registro.full_clean()
    registro.save()
    return registro


def _mensagens(resposta):
    return " ".join(str(m) for m in resposta.context["messages"])


@pytest.fixture
def limitado(db):
    veiculo = Veiculo.objects.create(placa="REV1D23", marca_modelo="Gol", km_atual=25_000)
    cliente = Cliente.objects.create(nome="Lucas", cpf_cnpj="52998224725")
    Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 5, 20),
        km_entrega=35_000,
        valor_semanal=Decimal("650.00"),
        limite_km=Alocacao.LimiteKm.LIMITADO,
        franquia_km_mensal=500,
        taxa_km_excedido=Decimal("5.00"),
    )
    return veiculo


def _registrar(cliente_http, veiculo, data, km, mes):
    return cliente_http.post(
        f"/km/registrar/{veiculo.pk}/",
        {"data_leitura": data.isoformat(), "km": km, "mes": mes},
        follow=True,
    )


# --- mês esquecido com falha no excedente --------------------------------------


def test_mes_esquecido_com_excedente_que_falha_nao_diz_que_refez(
    usuario_logado, limitado, monkeypatch
):
    _registrar(usuario_logado, limitado, date(2026, 9, 10), "38000", "2026-09")
    setembro = RegistroKm.objects.get(veiculo=limitado, mes_referencia=date(2026, 9, 1))
    cobranca = setembro.cobranca_excedente
    assert cobranca.valor == Decimal("5585.00")

    def falha(_registro):
        raise RuntimeError("contrato sem taxa")

    monkeypatch.setattr("apps.km.views.gerar_cobrancas_excedente", falha)
    resposta = _registrar(usuario_logado, limitado, date(2026, 8, 31), "37500", "2026-08")

    texto = _mensagens(resposta)
    assert "a cobrança de excedente de km não saiu agora" in texto
    # a cobrança de setembro voltou como estava (o atomic desfez o "refazer"): dizer
    # "era R$ 5.585,00, agora é R$ 1.665,00" seria mentira
    assert "era R$" not in texto
    cobranca.refresh_from_db()
    assert cobranca.status != Cobranca.Status.CANCELADA
    setembro.refresh_from_db()
    assert setembro.cobranca_excedente_id == cobranca.pk
    # nada cobrado em dobro: a de setembro já cobria desde a entrega
    assert (
        Cobranca.objects.filter(origem=Cobranca.Origem.EXCEDENTE_KM)
        .exclude(status=Cobranca.Status.CANCELADA)
        .count()
        == 1
    )


# --- correção da 1ª leitura: KM de compra sem data ---------------------------


@pytest.fixture
def compra_sem_data(db):
    return Veiculo.objects.create(
        placa="CSD1A23", marca_modelo="Gol", km_atual=30_000, km_compra=30_000
    )


def test_corrigir_primeira_leitura_para_menos_que_a_compra_sem_data_avisa(compra_sem_data):
    registro = _leitura(compra_sem_data, date(2026, 9, 10), 31_000)
    registro.km = 3_100
    aviso = suspeita_do_km(registro)
    assert aviso is not None
    assert "30.000 km (cadastro do carro)" in aviso
    assert "o número é menor" in aviso


def test_corrigir_primeira_leitura_com_zero_a_mais_e_compra_sem_data_avisa(compra_sem_data):
    registro = _leitura(compra_sem_data, date(2026, 9, 10), 31_000)
    registro.km = 310_000
    aviso = suspeita_do_km(registro)
    assert aviso is not None
    assert "280.000 km a mais" in aviso


def test_corrigir_primeira_leitura_normal_com_compra_sem_data_nao_avisa(compra_sem_data):
    registro = _leitura(compra_sem_data, date(2026, 9, 10), 31_000)
    registro.km = 31_500
    assert suspeita_do_km(registro) is None


# --- "Última": KM atual subido à mão na ficha ------------------------------------


def _mes(data):
    return f"{data:%Y-%m}"


def test_ultima_do_mes_atual_mostra_o_km_subido_na_ficha(usuario_logado, db):
    hoje = timezone.localdate()
    veiculo = Veiculo.objects.create(placa="FIC1A23", marca_modelo="Gol", km_atual=40_000)
    anterior = hoje.replace(day=1) - timedelta(days=10)
    _leitura(veiculo, anterior, 40_000)
    # Paulo subiu o KM na ficha do carro (sem lançamento)
    Veiculo.objects.filter(pk=veiculo.pk).update(km_atual=47_000)

    html = usuario_logado.get(f"/km/?mes={_mes(hoje)}").content.decode()
    assert "47.000 km</strong>" in html
    assert "KM atual da ficha do carro" in html


def test_ultima_do_mes_atual_prefere_o_lancamento_quando_o_km_e_o_mesmo(usuario_logado, db):
    hoje = timezone.localdate()
    veiculo = Veiculo.objects.create(placa="FIC2A23", marca_modelo="Gol", km_atual=40_000)
    anterior = hoje.replace(day=1) - timedelta(days=10)
    _leitura(veiculo, anterior, 41_000)

    html = usuario_logado.get(f"/km/?mes={_mes(hoje)}").content.decode()
    assert "41.000 km</strong>" in html
    assert "KM atual da ficha do carro" not in html
    assert f"em {anterior:%d/%m/%Y}" in html


def test_ultima_de_mes_passado_nao_usa_o_km_da_ficha(usuario_logado, db):
    veiculo = Veiculo.objects.create(placa="FIC3A23", marca_modelo="Gol", km_atual=40_000)
    _leitura(veiculo, date(2026, 6, 10), 40_000)
    Veiculo.objects.filter(pk=veiculo.pk).update(km_atual=90_000)

    html = usuario_logado.get("/km/?mes=2026-07").content.decode()
    assert "40.000 km</strong>" in html
    assert "90.000" not in html
    assert "KM atual da ficha do carro" not in html


# --- lista: excedente de quem devolveu antes não consulta cliente por linha ------


def _leitura_com_acerto(indice):
    from apps.km.models import AcertoExcedenteKm

    veiculo = Veiculo.objects.create(placa=f"LST{indice}A23", marca_modelo="Gol", km_atual=10_000)
    cpf = ["52998224725", "11144477735", "39053344705"][indice]
    cliente = Cliente.objects.create(nome=f"Cliente {indice}", cpf_cnpj=cpf)
    alocacao = Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 8, 1),
        data_termino=date(2026, 8, 20),
        km_entrega=10_000,
        valor_semanal=Decimal("650.00"),
    )
    registro = _leitura(veiculo, date(2026, 9, 10), 12_000)
    cobranca = Cobranca.objects.create(
        cliente=cliente,
        alocacao=alocacao,
        origem=Cobranca.Origem.EXCEDENTE_KM,
        descricao=f"Excedente de km {veiculo.placa} 09/2026 — 100 km além da franquia de 500 km",
        valor=Decimal("500.00"),
        vencimento=date(2026, 9, 17),
    )
    AcertoExcedenteKm.objects.create(registro=registro, alocacao=alocacao, cobranca=cobranca)


def test_lista_do_mes_nao_consulta_o_cliente_do_acerto_por_linha(usuario_logado, db):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    _leitura_com_acerto(0)
    usuario_logado.get("/km/?mes=2026-09")  # aquece sessão
    with CaptureQueriesContext(connection) as uma:
        html = usuario_logado.get("/km/?mes=2026-09").content.decode()
    assert "excedente de Cliente 0: 100 km além da franquia de 500 km = R$ 500,00" in html
    _leitura_com_acerto(1)
    _leitura_com_acerto(2)
    with CaptureQueriesContext(connection) as tres:
        usuario_logado.get("/km/?mes=2026-09")
    assert len(tres.captured_queries) == len(uma.captured_queries)
