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


@pytest.mark.django_db
def test_venda_aceita_valor_no_formato_brasileiro(usuario_logado):
    veiculo = Veiculo.objects.create(placa="VND1A23", marca_modelo="Onix")
    resposta = usuario_logado.post(
        f"/frota/veiculo/{veiculo.pk}/vender/",
        {"data": "2026-09-01", "valor": "45.000,00", "custos": "R$ 1.200,50", "km": "80000"},
    )
    assert resposta.status_code == 302
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.VENDIDO
    assert str(veiculo.valor_venda) == "45000.00"


@pytest.mark.django_db
def test_telas_secundarias_da_frota_renderizam(usuario_logado):
    veiculo = Veiculo.objects.create(placa="TLA1B23", marca_modelo="Gol")
    Categoria.objects.create(nome="Hatch", valor_semanal_referencia="1250.00")
    venda = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/vender/").content.decode()
    assert "não pode ser desfeita" in venda and "confirm(" in venda
    categorias = usuario_logado.get("/frota/categorias/").content.decode()
    assert 'value="1.250,00"' in categorias
    assert "Como ler esta tela" in usuario_logado.get("/frota/desmobilizacao/").content.decode()
    assert usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/ficha/").status_code == 200
    assert usuario_logado.get("/frota/fornecedores/").status_code == 200
    busca = usuario_logado.get("/buscar/?q=zzzz").content.decode()
    assert "Nenhum resultado" in busca
