import pytest
from django.db import IntegrityError

from apps.frota.models import Categoria, Veiculo


@pytest.mark.django_db
def test_placa_normalizada_ao_salvar():
    veiculo = Veiculo.objects.create(placa="qxq-6c10", marca_modelo="Gol")
    assert veiculo.placa == "QXQ6C10"


@pytest.mark.django_db
def test_placa_unica():
    Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol")
    with pytest.raises(IntegrityError):
        Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol")


@pytest.mark.django_db
def test_veiculo_nasce_disponivel_para_locacao():
    veiculo = Veiculo.objects.create(placa="RGD6H42", marca_modelo="Gol")
    assert veiculo.status == Veiculo.Status.DISPONIVEL
    assert veiculo.uso == Veiculo.Uso.LOCACAO


@pytest.mark.django_db
def test_historico_registra_alteracoes():
    categoria = Categoria.objects.create(nome="Hatch", valor_semanal_referencia=650)
    categoria.valor_semanal_referencia = 700
    categoria.save()
    assert categoria.history.count() == 2
