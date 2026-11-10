"""Etapa 18b — achados dos testes no navegador na área de KM.

Mês esquecido que deixava o excedente do mês seguinte cobrado em dobro, leitura
depois da venda, "Última" de mês passado com número do futuro, KM com "km" no
fim, correção que não dizia o que faltava e textos sem ponto de milhar.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.alocacoes.models import Alocacao
from apps.financeiro.models import Cobranca
from apps.frota.models import Veiculo
from apps.km.forms import km_digitado
from apps.km.models import RegistroKm
from apps.pessoas.models import Cliente

HTMX = {"HTTP_HX_REQUEST": "true"}


def _leitura(veiculo, data, km):
    registro = RegistroKm(
        veiculo=veiculo, mes_referencia=data.replace(day=1), data_leitura=data, km=km
    )
    registro.full_clean()
    registro.save()
    return registro


def _mensagens(resposta):
    return " ".join(str(m) for m in resposta.context["messages"])


# --- KM digitado com "km" no fim / vazio ---------------------------------------


@pytest.mark.parametrize("texto", ["35.500 km", "35500km", "35.500 KM", " 35 500 Km "])
def test_km_com_sufixo_km_e_aceito(texto):
    assert km_digitado(texto) == 35_500


def test_km_vazio_pede_o_km_do_painel():
    with pytest.raises(ValidationError) as erro:
        km_digitado("")
    assert erro.value.messages == ["Informe o KM do painel."]


def test_km_torto_continua_recusado():
    with pytest.raises(ValidationError):
        km_digitado("12.5 km")


# --- mês esquecido refaz o excedente do mês seguinte ---------------------------


@pytest.fixture
def limitado(db):
    veiculo = Veiculo.objects.create(placa="ABC1D23", marca_modelo="Gol", km_atual=25_000)
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


def _registrar(cliente_http, veiculo, data, km, mes, **extra):
    return cliente_http.post(
        f"/km/registrar/{veiculo.pk}/",
        {"data_leitura": data.isoformat(), "km": km, "mes": mes, **extra},
        follow=True,
    )


def _excedentes_ativos(veiculo):
    return Cobranca.objects.filter(
        origem=Cobranca.Origem.EXCEDENTE_KM, alocacao__veiculo=veiculo
    ).exclude(status=Cobranca.Status.CANCELADA)


def test_mes_esquecido_refaz_o_excedente_ja_cobrado_do_mes_seguinte(usuario_logado, limitado):
    _registrar(usuario_logado, limitado, date(2026, 9, 10), "38000", "2026-09")
    setembro = RegistroKm.objects.get(veiculo=limitado, mes_referencia=date(2026, 9, 1))
    assert setembro.cobranca_excedente.valor == Decimal("5585.00")

    resposta = _registrar(usuario_logado, limitado, date(2026, 8, 31), "37500", "2026-08")

    setembro.refresh_from_db()
    assert setembro.cobranca_excedente.valor == Decimal("1665.00")
    agosto = RegistroKm.objects.get(veiculo=limitado, mes_referencia=date(2026, 8, 1))
    assert agosto.cobranca_excedente.valor == Decimal("3915.00")
    # 3.000 km rodados desde a entrega: nada cobrado em dobro
    assert sum(c.valor for c in _excedentes_ativos(limitado)) == Decimal("5580.00")
    texto = _mensagens(resposta)
    assert "era R$ 5.585,00, agora é R$ 1.665,00" in texto
    assert "duplicidade" not in texto


def test_mes_esquecido_com_excedente_pago_so_avisa(usuario_logado, limitado):
    _registrar(usuario_logado, limitado, date(2026, 9, 10), "38000", "2026-09")
    setembro = RegistroKm.objects.get(veiculo=limitado, mes_referencia=date(2026, 9, 1))
    cobranca = setembro.cobranca_excedente
    Cobranca.objects.filter(pk=cobranca.pk).update(status=Cobranca.Status.JUDICIAL)

    resposta = _registrar(usuario_logado, limitado, date(2026, 8, 31), "37500", "2026-08")

    setembro.refresh_from_db()
    assert setembro.cobranca_excedente_id == cobranca.pk
    assert "não foi mexida" in _mensagens(resposta)


def test_descricao_e_selo_do_excedente_tem_milhar(usuario_logado, limitado):
    _registrar(usuario_logado, limitado, date(2026, 9, 10), "38000", "2026-09")
    cobranca = RegistroKm.objects.get(veiculo=limitado).cobranca_excedente
    assert "1.117 km além da franquia de 1.883 km" in cobranca.descricao

    html = usuario_logado.get("/km/?mes=2026-09").content.decode()
    assert "1.117 km além da franquia de 1.883 km = R$ 5.585,00" in html


def test_mensagem_da_primeira_leitura_sem_parenteses_dentro_de_parenteses(usuario_logado, limitado):
    resposta = _registrar(usuario_logado, limitado, date(2026, 9, 10), "38000", "2026-09")
    texto = _mensagens(resposta)
    assert "rodou 3.000 km desde a entrega, em 20/05, com 35.000 km" in texto
    assert "((" not in texto and "))" not in texto


# --- carro vendido -------------------------------------------------------------


@pytest.fixture
def vendido(db):
    return Veiculo.objects.create(
        placa="MNB9K78",
        marca_modelo="Gol",
        km_atual=183_938,
        status=Veiculo.Status.VENDIDO,
        data_venda=date(2026, 10, 16),
    )


def test_leitura_depois_da_venda_e_recusada(vendido):
    registro = RegistroKm(
        veiculo=vendido,
        mes_referencia=date(2026, 11, 1),
        data_leitura=date(2026, 11, 10),
        km=184_000,
    )
    with pytest.raises(ValidationError) as erro:
        registro.full_clean()
    assert "foi vendido em 16/10/2026" in str(erro.value)


def test_registrar_depois_da_venda_devolve_a_linha_com_o_erro(usuario_logado, vendido):
    resposta = usuario_logado.post(
        f"/km/registrar/{vendido.pk}/",
        {"data_leitura": "2026-11-10", "km": "184000", "mes": "2026-11"},
        **HTMX,
    )
    assert "foi vendido em 16/10/2026" in resposta.content.decode()
    assert not RegistroKm.objects.filter(veiculo=vendido).exists()


def test_leitura_ate_o_dia_da_venda_continua_aceita(vendido):
    _leitura(vendido, date(2026, 10, 16), 184_000)


# --- "Última" de um mês passado ------------------------------------------------


def test_ultima_de_mes_passado_nao_mostra_leitura_do_futuro(usuario_logado, db):
    veiculo = Veiculo.objects.create(
        placa="TST1E11", marca_modelo="Gol", km_atual=50_000, km_compra=50_000
    )
    _leitura(veiculo, date(2026, 9, 10), 52_000)
    html = usuario_logado.get("/km/?mes=2026-08").content.decode()
    assert "50.000 km</strong>" in html
    assert "KM da compra" in html
    assert "52.000" not in html
    assert "KM do cadastro" not in html


def test_ultima_de_mes_passado_usa_entrega_de_antes_do_mes(usuario_logado, limitado):
    _leitura(limitado, date(2026, 9, 10), 38_000)
    html = usuario_logado.get("/km/?mes=2026-08").content.decode()
    assert "35.000 km</strong>" in html
    assert "em 20/05/2026 (entrega)" in html
    assert "38.000" not in html


def test_carro_sem_nenhum_lancamento_mostra_o_km_do_cadastro(usuario_logado, db):
    Veiculo.objects.create(placa="CAD1A23", marca_modelo="Gol", km_atual=41_000)
    html = usuario_logado.get("/km/?mes=2026-08").content.decode()
    assert "41.000 km</strong>" in html
    assert "KM do cadastro (sem leitura anterior)" in html


def test_leitura_repetida_mostra_a_leitura_ja_gravada_como_ultima(usuario_logado, db):
    veiculo = Veiculo.objects.create(placa="TLP8G47", marca_modelo="Gol", km_atual=30_000)
    dados = {"data_leitura": "2026-09-10", "km": "35500", "mes": "2026-09"}
    usuario_logado.post(f"/km/registrar/{veiculo.pk}/", dados, **HTMX)
    html = usuario_logado.post(f"/km/registrar/{veiculo.pk}/", dados, **HTMX).content.decode()
    assert "já tem leitura em 09/2026" in html
    assert "35.500 km</strong>" in html
    assert "em 10/09/2026" in html
    assert "KM do cadastro" not in html


# --- Corrigir ------------------------------------------------------------------


def test_corrigir_diz_que_falta_marcar_a_caixa_e_usa_a_entrega_como_base(usuario_logado, limitado):
    limitado.km_compra = 25_000
    limitado.save()
    registro = _leitura(limitado, date(2026, 9, 10), 36_000)
    url = f"/km/corrigir/{registro.pk}/"
    dados = {"data_leitura": "2026-09-10", "km": "380000", "observacoes": ""}

    primeira = usuario_logado.post(url, dados).content.decode()
    assert "o carro já marcou 35.000 km (entrega de 20/05)" in primeira
    assert "345.000 km a mais" in primeira
    assert "Marque “Sim, o número está certo” para gravar" not in primeira

    segunda = usuario_logado.post(url, {**dados, "avisado": "380000"}).content.decode()
    assert "Marque “Sim, o número está certo” para gravar" in segunda
    registro.refresh_from_db()
    assert registro.km == 36_000


def test_corrigir_so_a_data_diz_que_a_data_mudou(usuario_logado, db):
    veiculo = Veiculo.objects.create(placa="TST2E11", marca_modelo="Gol", km_atual=50_000)
    registro = _leitura(veiculo, date(2026, 9, 10), 52_000)
    resposta = usuario_logado.post(
        f"/km/corrigir/{registro.pk}/",
        {"data_leitura": "2026-09-01", "km": "52.000", "observacoes": ""},
        follow=True,
    )
    texto = _mensagens(resposta)
    assert "corrigida: data 10/09 → 01/09." in texto
    assert "52.000 → 52.000" not in texto


def test_corrigir_o_km_diz_o_km_antigo_e_o_novo(usuario_logado, db):
    veiculo = Veiculo.objects.create(placa="TST3E11", marca_modelo="Gol", km_atual=50_000)
    registro = _leitura(veiculo, date(2026, 9, 10), 52_000)
    resposta = usuario_logado.post(
        f"/km/corrigir/{registro.pk}/",
        {"data_leitura": "2026-09-10", "km": "51.500", "observacoes": "foto do painel"},
        follow=True,
    )
    assert "corrigida: KM 52.000 → 51.500, observação atualizada." in _mensagens(resposta)


def test_str_da_leitura_tem_milhar(db):
    veiculo = Veiculo.objects.create(placa="STR1A23", marca_modelo="Gol", km_atual=10_000)
    assert str(_leitura(veiculo, date(2026, 9, 10), 19_510)).endswith("19.510 km")
