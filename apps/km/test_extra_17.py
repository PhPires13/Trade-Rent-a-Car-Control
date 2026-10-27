"""Etapa 17 (achados extras) — frente km-manutenção: leitura mensal de KM."""

import inspect
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.alocacoes import km_aviso
from apps.alocacoes.models import Alocacao
from apps.frota.models import Veiculo
from apps.km import correcao
from apps.km.correcao import suspeita_do_km
from apps.km.models import RegistroKm
from apps.km.views import _anotar_maior_km, _pendentes_anotados
from apps.manutencao.models import Manutencao
from apps.pessoas.models import Cliente

HOJE = timezone.localdate()


def _leitura(veiculo, data_leitura, km):
    registro = RegistroKm(
        veiculo=veiculo,
        mes_referencia=data_leitura.replace(day=1),
        data_leitura=data_leitura,
        km=km,
    )
    registro.full_clean()
    registro.save()
    return registro


def _alocar(veiculo, inicio, km_entrega):
    cliente = Cliente.objects.create(nome="Lucas", cpf_cnpj="52998224725")
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=inicio,
        km_entrega=km_entrega,
        valor_semanal=Decimal("650.00"),
        limite_km=Alocacao.LimiteKm.LIMITADO,
        franquia_km_mensal=1_000,
        taxa_km_excedido=Decimal("1.75"),
    )


# --- correção com KM menor que o da entrega ----------------------------------


def test_corrigir_para_menos_que_a_entrega_pede_confirmacao(usuario_logado, db):
    veiculo = Veiculo.objects.create(placa="ABC1D17", marca_modelo="Gol", km_atual=30_000)
    _alocar(veiculo, HOJE - timedelta(days=40), 35_000)
    registro = _leitura(veiculo, HOJE - timedelta(days=2), 37_000)
    url = f"/km/corrigir/{registro.pk}/"
    dados = {"data_leitura": registro.data_leitura.isoformat(), "km": "34000"}

    resposta = usuario_logado.post(url, dados)

    assert resposta.status_code == 200
    assert "35.000 km (entrega de" in resposta.context["aviso"]
    registro.refresh_from_db()
    assert registro.km == 37_000
    # confirmando, grava
    assert usuario_logado.post(url, {**dados, "confirmo": "34000"}).status_code == 302
    registro.refresh_from_db()
    assert registro.km == 34_000


def test_corrigir_acima_da_entrega_nao_avisa(db):
    veiculo = Veiculo.objects.create(placa="ABC2D17", marca_modelo="Gol", km_atual=30_000)
    _alocar(veiculo, HOJE - timedelta(days=40), 35_000)
    registro = _leitura(veiculo, HOJE - timedelta(days=2), 37_000)
    registro.km = 36_000
    assert suspeita_do_km(registro) is None


# --- aviso de salto cita o maior KM da linha ---------------------------------


def test_aviso_de_salto_cita_o_km_da_manutencao(db):
    veiculo = Veiculo.objects.create(placa="RTD4E17", marca_modelo="Gol", km_atual=60_000)
    _leitura(veiculo, HOJE - timedelta(days=27), 63_080)
    Manutencao.objects.create(
        veiculo=veiculo, tipo="corretiva", data=HOJE - timedelta(days=10), km=65_180, descricao="x"
    )
    registro = RegistroKm(
        veiculo=Veiculo.objects.get(pk=veiculo.pk),
        mes_referencia=HOJE.replace(day=1),
        data_leitura=HOJE,
        km=651_800,
    )
    if registro.mes_referencia == (HOJE - timedelta(days=27)).replace(day=1):
        pytest.skip("as duas leituras cairiam no mesmo mês")
    aviso = suspeita_do_km(registro)
    assert "63.080 km" in aviso
    assert "já marcou 65.180 km (manutenção de" in aviso


# --- primeira leitura com um zero a mais -------------------------------------


def test_primeira_leitura_dez_vezes_o_cadastro_avisa(db):
    veiculo = Veiculo.objects.create(placa="AB1C017", marca_modelo="Gol", km_atual=1_000)
    registro = RegistroKm(
        veiculo=veiculo, mes_referencia=HOJE.replace(day=1), data_leitura=HOJE, km=10_000
    )
    assert "seriam 9.000 km a mais" in suspeita_do_km(registro)


def test_primeira_leitura_passa_da_media_diaria_desde_a_compra_avisa(db):
    veiculo = Veiculo.objects.create(
        placa="AB2C017",
        marca_modelo="Gol",
        km_atual=35,
        km_compra=35,
        data_aquisicao=HOJE - timedelta(days=3),
    )
    registro = RegistroKm(
        veiculo=veiculo, mes_referencia=HOJE.replace(day=1), data_leitura=HOJE, km=9_000
    )
    assert suspeita_do_km(registro) is not None


def test_primeira_leitura_de_carro_zero_normal_nao_avisa(db):
    veiculo = Veiculo.objects.create(
        placa="AB3C017",
        marca_modelo="Gol",
        km_atual=35,
        km_compra=35,
        data_aquisicao=HOJE - timedelta(days=30),
    )
    registro = RegistroKm(
        veiculo=veiculo, mes_referencia=HOJE.replace(day=1), data_leitura=HOJE, km=4_000
    )
    assert suspeita_do_km(registro) is None


# --- KM rodado da 1ª leitura (desde a entrega) -------------------------------


def test_primeira_leitura_mostra_km_desde_a_entrega(usuario_logado, db):
    veiculo = Veiculo.objects.create(placa="ABC3D17", marca_modelo="Gol", km_atual=30_000)
    _alocar(veiculo, HOJE - timedelta(days=20), 35_000)

    resposta = usuario_logado.post(
        f"/km/registrar/{veiculo.pk}/",
        {"data_leitura": HOJE.isoformat(), "km": "37000", "mes": HOJE.strftime("%Y-%m")},
        follow=True,
    )

    html = resposta.content.decode()
    texto = f"2.000 km desde a entrega (35.000 em {HOJE - timedelta(days=20):%d/%m})"
    assert f"rodou {texto}" in html
    # na lista o texto ficou de fora: custava consultas por linha (pendente p/ fazer em lote)


# --- "Última" de um mês passado não pega o 6º dia do mês seguinte ------------


def test_ultima_do_mes_ignora_o_sexto_dia_do_mes_seguinte(db):
    veiculo = Veiculo.objects.create(placa="LIM6D17", marca_modelo="Gol", km_atual=500)
    _leitura(veiculo, date(2026, 9, 30), 1_000)
    Manutencao.objects.create(
        veiculo=veiculo, tipo="corretiva", data=date(2026, 11, 6), km=2_000, descricao="x"
    )
    veiculos = _anotar_maior_km(
        list(_pendentes_anotados(date(2026, 10, 1), Veiculo.objects.filter(pk=veiculo.pk))),
        date(2026, 10, 1),
    )
    assert veiculos[0].maior_km == 1_000


def test_ultima_do_mes_aceita_o_quinto_dia_do_mes_seguinte(db):
    veiculo = Veiculo.objects.create(placa="LIM5D17", marca_modelo="Gol", km_atual=500)
    _leitura(veiculo, date(2026, 9, 30), 1_000)
    Manutencao.objects.create(
        veiculo=veiculo, tipo="corretiva", data=date(2026, 11, 5), km=2_000, descricao="x"
    )
    veiculos = _anotar_maior_km(
        list(_pendentes_anotados(date(2026, 10, 1), Veiculo.objects.filter(pk=veiculo.pk))),
        date(2026, 10, 1),
    )
    assert veiculos[0].maior_km == 2_000


# --- texto do topo e helper único --------------------------------------------


def test_texto_do_topo_diz_que_menor_que_a_leitura_anterior_nao_e_aceito(usuario_logado):
    Veiculo.objects.create(placa="TXT1A17", marca_modelo="Gol", km_atual=1_000)
    html = usuario_logado.get("/km/").content.decode()
    assert "Número menor que a leitura do mês anterior não é aceito" in html


def test_ha_quanto_tempo_tem_uma_copia_so():
    assert "def _ha_quanto_tempo" not in inspect.getsource(km_aviso)
    assert correcao._ha_quanto_tempo(1) == "há 1 dia"
