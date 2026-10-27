"""Qualidade final do KM: leitura menor que o KM atual, ano torto, médias e Admin."""

from datetime import timedelta

import pytest
from django.contrib import admin
from django.utils import timezone

from apps.frota.models import Veiculo
from apps.km.correcao import ajustar_km_atual, origem_do_km, suspeita_do_km
from apps.km.forms import CorrecaoLeituraForm
from apps.km.models import RegistroKm
from apps.manutencao.models import Manutencao

HOJE = timezone.localdate()
MES = HOJE.replace(day=1)
MES_PASSADO = MES - timedelta(days=5)


@pytest.fixture
def carro(db):
    """RTA1B02: leitura de 77.300 no mês passado e manutenção com 82.100 neste mês."""
    veiculo = Veiculo.objects.create(
        placa="RTA1B02", marca_modelo="Onix", km_compra=70_000, km_atual=70_000
    )
    leitura = RegistroKm(
        veiculo=veiculo,
        mes_referencia=MES_PASSADO.replace(day=1),
        data_leitura=MES_PASSADO,
        km=77_300,
    )
    leitura.full_clean()
    leitura.save()
    Manutencao.objects.create(
        veiculo=veiculo, tipo="corretiva", data=MES, km=82_100, descricao="Freio"
    )
    veiculo.refresh_from_db()
    assert veiculo.km_atual == 82_100
    return veiculo


def _nova(veiculo, km, data=HOJE):
    registro = RegistroKm(veiculo=veiculo, mes_referencia=MES, data_leitura=data, km=km)
    registro.full_clean()
    return registro


def test_leitura_menor_que_o_km_da_manutencao_avisa(carro):
    aviso = suspeita_do_km(_nova(carro, 80_000))
    assert aviso.startswith("Você digitou 80.000 km, mas o carro já marcou 82.100 km")
    assert f"(manutenção de {MES:%d/%m})" in aviso
    assert "Confira o painel" in aviso


def test_leitura_igual_ou_maior_que_o_km_atual_nao_avisa(carro):
    assert suspeita_do_km(_nova(carro, 82_100)) is None
    assert suspeita_do_km(_nova(carro, 83_000)) is None


def test_origem_do_km(carro):
    assert origem_do_km(carro, 82_100) == ("manutenção", MES)
    assert origem_do_km(carro, 77_300) == ("leitura mensal", MES_PASSADO)
    assert origem_do_km(carro, 1) == ("cadastro", None)


def test_tela_exige_confirmacao_para_leitura_menor(usuario_logado, carro):
    url = f"/km/registrar/{carro.pk}/"
    dados = {"km": "80000", "data_leitura": HOJE.isoformat(), "mes": f"{MES:%Y-%m}"}
    resposta = usuario_logado.post(url, dados, HTTP_HX_REQUEST="true")
    assert "já marcou 82.100 km" in resposta.content.decode()
    assert not RegistroKm.objects.filter(veiculo=carro, mes_referencia=MES).exists()

    usuario_logado.post(url, {**dados, "confirmo": "80000"}, HTTP_HX_REQUEST="true")
    assert RegistroKm.objects.get(veiculo=carro, mes_referencia=MES).km == 80_000


def test_ultima_mostra_o_maior_km_conhecido_e_a_origem(usuario_logado, carro):
    html = usuario_logado.get(f"/km/?mes={MES:%Y-%m}").content.decode()
    assert "82.100 km" in html
    assert "(manutenção)" in html
    assert "77.300 km" not in html


def test_ano_torto_na_leitura_nao_da_erro_500(usuario_logado, carro):
    resposta = usuario_logado.post(
        f"/km/registrar/{carro.pk}/",
        {"km": "90000", "data_leitura": "0001-01-01", "mes": f"{MES:%Y-%m}"},
        HTTP_HX_REQUEST="true",
    )
    assert resposta.status_code == 200
    assert "Confira o ano" in resposta.content.decode()


def test_ano_torto_na_correcao_vira_erro_do_formulario(carro):
    registro = RegistroKm.objects.get(veiculo=carro)
    form = CorrecaoLeituraForm(
        {"km": "77300", "data_leitura": "0001-01-01", "observacoes": ""}, instance=registro
    )
    assert not form.is_valid()
    assert "Confira o ano" in str(form.errors["data_leitura"])


def test_media_mes_tem_separador_de_milhar(usuario_logado, db):
    veiculo = Veiculo.objects.create(placa="MED1A00", marca_modelo="Gol", km_atual=10_000)
    for data, km in ((MES_PASSADO - timedelta(days=30), 10_000), (MES_PASSADO, 20_000)):
        registro = RegistroKm(
            veiculo=veiculo, mes_referencia=data.replace(day=1), data_leitura=data, km=km
        )
        registro.full_clean()
        registro.save()
    html = usuario_logado.get(f"/km/?mes={MES_PASSADO:%Y-%m}").content.decode()
    assert "10.000" in html  # média/mês: 10.000 km em 30 dias
    assert ">10000<" not in html


def test_ajustar_km_atual_e_publica(carro):
    Manutencao.objects.filter(veiculo=carro).update(km=79_000)
    assert ajustar_km_atual(carro, 82_100) == "O KM atual de RTA1B02 voltou para 79.000 km."


def test_admin_nao_monta_select_de_cobrancas():
    registro_admin = admin.site._registry[RegistroKm]
    assert "cobranca_excedente" in registro_admin.raw_id_fields
    assert "veiculo" in registro_admin.raw_id_fields


def test_lista_de_km_busca_a_origem_em_lote(usuario_logado, django_assert_max_num_queries, db):
    """'Última' com a origem do KM: eram 7 consultas por carro pendente."""
    for indice in range(12):
        veiculo = Veiculo.objects.create(
            placa=f"LOT{indice:04d}"[:7], marca_modelo="Onix", km_compra=10_000, km_atual=10_000
        )
        Manutencao.objects.create(
            veiculo=veiculo,
            tipo="corretiva",
            data=MES_PASSADO,
            km=20_000 + indice,
            descricao="Freio",
        )
    with django_assert_max_num_queries(25):
        resposta = usuario_logado.get(f"/km/?mes={HOJE:%Y-%m}")
    html = resposta.content.decode()
    assert resposta.status_code == 200
    assert "20.011 km" in html
    assert "(manutenção)" in html


def test_origens_do_km_em_lote_igual_a_uma_a_uma(carro):
    from apps.km.correcao import origens_do_km

    outro = Veiculo.objects.create(placa="OUT1A11", marca_modelo="Gol", km_atual=1_000)
    origens = origens_do_km({carro.pk: carro.km_atual, outro.pk: outro.km_atual})
    assert origens[carro.pk] == origem_do_km(carro, carro.km_atual) == ("manutenção", MES)
    assert origens[outro.pk] == ("cadastro", None)
