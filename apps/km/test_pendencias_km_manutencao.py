"""Pendências da etapa 15 — frente km-manutenção: contador "faltam" e Admin do acerto."""

from datetime import date

import pytest
from django.contrib import admin

from apps.frota.models import Veiculo
from apps.km.models import AcertoExcedenteKm, pendencias_de_leitura, veiculos_com_leitura_pendente

HOJE = date.today()
MES = HOJE.replace(day=1)


def _mes_anterior(mes):
    return (mes.replace(day=1) - date.resolution).replace(day=1)


@pytest.mark.django_db
def test_carro_comprado_depois_do_mes_nao_fica_pendente():
    """Mesma regra do painel: mês anterior à compra não cobra leitura."""
    Veiculo.objects.create(placa="ANT1G01", marca_modelo="Gol", data_aquisicao=None)
    Veiculo.objects.create(placa="NOV1C02", marca_modelo="Onix", data_aquisicao=HOJE)
    anterior = _mes_anterior(MES)
    assert list(veiculos_com_leitura_pendente(anterior).values_list("placa", flat=True)) == [
        "ANT1G01"
    ]
    assert veiculos_com_leitura_pendente(MES).count() == 2
    assert {p.veiculo.placa for p in pendencias_de_leitura(HOJE) if p.mes == MES} == {
        "ANT1G01",
        "NOV1C02",
    }


@pytest.mark.django_db
def test_contador_do_htmx_nao_conta_carro_ainda_nao_comprado(usuario_logado):
    anterior = _mes_anterior(MES)
    lido = Veiculo.objects.create(placa="LID1D01", marca_modelo="Gol", km_atual=1_000)
    Veiculo.objects.create(placa="NOV1C02", marca_modelo="Onix", data_aquisicao=HOJE)
    resposta = usuario_logado.post(
        f"/km/registrar/{lido.pk}/",
        {"data_leitura": anterior.isoformat(), "km": "1500", "mes": anterior.strftime("%Y-%m")},
        HTTP_HX_REQUEST="true",
    )
    assert resposta.status_code == 200
    assert "tudo registrado" in resposta.content.decode()


def test_acerto_de_excedente_aparece_no_admin_so_para_consulta(rf):
    modelo_admin = admin.site._registry[AcertoExcedenteKm]
    pedido = rf.get("/")
    assert not modelo_admin.has_add_permission(pedido)
    assert not modelo_admin.has_change_permission(pedido)
    assert not modelo_admin.has_delete_permission(pedido)


def test_lista_do_admin_do_acerto_abre(admin_client):
    resposta = admin_client.get("/admin/km/acertoexcedentekm/")
    assert resposta.status_code == 200
