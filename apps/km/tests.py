from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from apps.frota.models import Veiculo
from apps.km.models import RegistroKm, veiculos_com_leitura_pendente


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_compra=50_000)


def registrar(veiculo, data_leitura, km):
    registro = RegistroKm(
        veiculo=veiculo,
        mes_referencia=data_leitura.replace(day=1),
        data_leitura=data_leitura,
        km=km,
    )
    registro.full_clean()
    registro.save()
    return registro


def test_primeiro_registro_usa_km_da_compra(veiculo):
    registro = registrar(veiculo, date(2026, 7, 15), 52_000)
    assert registro.km_anterior == 50_000
    assert registro.km_utilizado == 2_000


def test_km_ant_e_dias_vem_do_mes_anterior(veiculo):
    registrar(veiculo, date(2026, 6, 10), 52_000)
    registro = registrar(veiculo, date(2026, 7, 12), 55_200)
    assert registro.km_anterior == 52_000
    assert registro.dias == 32
    assert registro.km_utilizado == 3_200
    assert round(registro.media_dia) == 100
    assert round(registro.media_mes) == 3_000


def test_um_registro_por_veiculo_por_mes(veiculo):
    registrar(veiculo, date(2026, 7, 10), 52_000)
    with pytest.raises(IntegrityError):
        RegistroKm.objects.create(
            veiculo=veiculo,
            mes_referencia=date(2026, 7, 1),
            data_leitura=date(2026, 7, 20),
            km=53_000,
        )


def test_km_menor_que_anterior_bloqueado(veiculo):
    registrar(veiculo, date(2026, 6, 10), 52_000)
    with pytest.raises(ValidationError):
        registrar(veiculo, date(2026, 7, 10), 51_000)


def test_registro_atualiza_km_atual_do_veiculo(veiculo):
    registrar(veiculo, date(2026, 7, 10), 52_000)
    veiculo.refresh_from_db()
    assert veiculo.km_atual == 52_000


def test_leituras_pendentes_do_mes(veiculo, db):
    Veiculo.objects.create(placa="RGD6H42", marca_modelo="Gol")
    Veiculo.objects.create(placa="RVZ9J95", marca_modelo="HB20", uso=Veiculo.Uso.FORA_LOCACAO)
    Veiculo.objects.create(placa="SWH9E89", marca_modelo="Virtus", status=Veiculo.Status.VENDIDO)
    registrar(veiculo, date(2026, 7, 10), 52_000)

    pendentes = veiculos_com_leitura_pendente(date(2026, 7, 25))
    assert list(pendentes.values_list("placa", flat=True)) == ["RGD6H42"]


@pytest.fixture
def usuario_logado(client, django_user_model):
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    client.login(username="dono", password="senha-forte-123")
    return client


def test_telas_de_km_renderizam(usuario_logado, veiculo):
    registrar(veiculo, date(2026, 7, 10), 52_000)
    assert usuario_logado.get("/km/").status_code == 200
    assert usuario_logado.get(f"/km/historico/{veiculo.pk}/").status_code == 200


def test_registrar_km_pela_tela(usuario_logado, veiculo):
    resposta = usuario_logado.post(
        f"/km/registrar/{veiculo.pk}/",
        {"data_leitura": "2026-07-15", "km": "52000"},
    )
    assert resposta.status_code == 302
    assert veiculo.registros_km.count() == 1
