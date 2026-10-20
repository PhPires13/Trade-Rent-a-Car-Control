"""Lista de sinistros com consultas fixas e valores de auxílio validados.

Um valor como "99999999" passava pelo dinheiro_br, era gravado e a partir daí
a própria lista de sinistros dava erro ao ler o auxílio de volta do banco.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.frota.models import Veiculo
from apps.manutencao.models import Manutencao
from apps.sinistros.models import AuxilioMotorista, Sinistro


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol")


def _sinistros(veiculo, quantidade):
    hoje = date.today()
    criados = []
    for i in range(quantidade):
        sinistro = Sinistro.objects.create(
            veiculo=veiculo, data=hoje - timedelta(days=20 + i), envolvido="associado"
        )
        Manutencao.objects.create(
            veiculo=veiculo,
            tipo="corretiva",
            data=hoje,
            descricao="Funilaria",
            data_entrada=hoje - timedelta(days=10),
            sinistro=sinistro,
        )
        if i % 2:
            AuxilioMotorista.objects.create(sinistro=sinistro, status="solicitado")
        criados.append(sinistro)
    return criados


def test_lista_de_sinistros_nao_consulta_por_linha(
    usuario_logado, veiculo, django_assert_max_num_queries
):
    _sinistros(veiculo, 2)
    with django_assert_max_num_queries(10):
        assert usuario_logado.get("/sinistros/").status_code == 200
    _sinistros(veiculo, 10)
    with django_assert_max_num_queries(10):
        html = usuario_logado.get("/sinistros/").content.decode()
    assert "Registrar pedido" in html
    assert "Marcar recebido" in html


@pytest.mark.parametrize("digitado", ["99999999", "NaN", "1e10"])
def test_auxilio_com_valor_fora_do_campo_avisa_em_vez_de_quebrar(usuario_logado, veiculo, digitado):
    (sinistro,) = _sinistros(veiculo, 1)
    resposta = usuario_logado.post(
        f"/sinistros/{sinistro.pk}/auxilio/", {"valor": digitado}, follow=True
    )
    assert resposta.status_code == 200
    assert not sinistro.auxilios.exists()
    assert usuario_logado.get("/sinistros/").status_code == 200


def test_receber_auxilio_com_valor_invalido_mantem_o_solicitado(usuario_logado, veiculo):
    (sinistro,) = _sinistros(veiculo, 1)
    auxilio = AuxilioMotorista.objects.create(
        sinistro=sinistro, valor=Decimal("1518.00"), status="solicitado"
    )
    usuario_logado.post(f"/sinistros/auxilio/{auxilio.pk}/receber/", {"valor": "99999999"})
    auxilio.refresh_from_db()
    assert auxilio.status == "solicitado"
    assert auxilio.valor == Decimal("1518.00")
    assert usuario_logado.get("/sinistros/").status_code == 200


def test_auxilio_valores_brasileiros_e_vazio(usuario_logado, veiculo):
    (sinistro,) = _sinistros(veiculo, 1)
    usuario_logado.post(f"/sinistros/{sinistro.pk}/auxilio/", {"valor": "1.518,00"})
    auxilio = sinistro.auxilios.get()
    assert auxilio.valor == Decimal("1518.00")
    usuario_logado.post(f"/sinistros/auxilio/{auxilio.pk}/receber/", {"valor": " "})
    auxilio.refresh_from_db()
    assert auxilio.status == "recebido"
    assert auxilio.valor == Decimal("1518.00")  # vazio mantém o valor solicitado
