"""Telas de multas com número fixo de consultas e valores digitados validados.

A coluna "Repasse" lia a ND de cada multa com duas consultas por linha
(item + cobrança); com 50 multas por página eram ~100 consultas a mais.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.alocacoes.models import Alocacao
from apps.frota.models import Veiculo
from apps.multas import services
from apps.multas.models import Multa
from apps.pessoas.models import Cliente


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


@pytest.fixture
def veiculo(cliente):
    veiculo = Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol")
    Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 1, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
    )
    return veiculo


def _multas(veiculo, quantidade, inicio=date(2026, 2, 1)):
    return [
        Multa.objects.create(
            veiculo=veiculo,
            data_infracao=inicio + timedelta(days=i),
            descricao=f"Infração {i}",
            valor=Decimal("100.00"),
        )
        for i in range(quantidade)
    ]


def _consultas_da_lista(usuario_logado, django_assert_max_num_queries, limite):
    with django_assert_max_num_queries(limite):
        resposta = usuario_logado.get("/multas/")
    assert resposta.status_code == 200
    return resposta.content.decode()


def test_lista_de_multas_nao_consulta_por_linha(
    usuario_logado, veiculo, cliente, django_assert_max_num_queries
):
    em_nd = _multas(veiculo, 3)
    services.gerar_nd_de_multas(cliente, em_nd, date(2026, 3, 1))
    services.registrar_nic(em_nd[0], valor=Decimal("195.30"))
    _multas(veiculo, 3, inicio=date(2026, 4, 1))
    pouca = _consultas_da_lista(usuario_logado, django_assert_max_num_queries, 12)

    mais = _multas(veiculo, 10, inicio=date(2026, 5, 1))
    services.gerar_nd_de_multas(cliente, mais[:5], date(2026, 6, 1))
    services.registrar_nic(mais[0], valor=Decimal("195.30"))
    html = _consultas_da_lista(usuario_logado, django_assert_max_num_queries, 12)

    assert "Incluída na ND 001" in pouca
    assert "Incluída na ND 002" in html
    assert "A cobrar" in html
    assert html.count(">NIC</span>") == 2


def test_tela_de_gerar_nd_nao_consulta_por_multa(
    usuario_logado, veiculo, cliente, django_assert_max_num_queries
):
    multas = _multas(veiculo, 12)
    services.gerar_nd_de_multas(cliente, multas[:4], date(2026, 3, 1))
    with django_assert_max_num_queries(10):
        resposta = usuario_logado.get(f"/multas/gerar-nd/?cliente={cliente.pk}")
    assert len(resposta.context["multas"]) == 8  # só as "a cobrar"


def test_gerar_nd_pela_tela_cobra_so_as_marcadas(usuario_logado, veiculo, cliente):
    multas = _multas(veiculo, 3)
    resposta = usuario_logado.post(
        "/multas/gerar-nd/",
        {"cliente": cliente.pk, "multa": [multas[0].pk, multas[2].pk]},
    )
    assert resposta.status_code == 302
    assert [m.repasse for m in Multa.objects.order_by("data_infracao")] == [
        "Incluída na ND 001",
        "A cobrar",
        "Incluída na ND 001",
    ]


@pytest.mark.parametrize("digitado", ["99999999", "NaN", "1e10"])
def test_nic_com_valor_fora_do_campo_avisa_em_vez_de_quebrar(usuario_logado, veiculo, digitado):
    (multa,) = _multas(veiculo, 1)
    resposta = usuario_logado.post(f"/multas/{multa.pk}/nic/", {"valor": digitado}, follow=True)
    assert resposta.status_code == 200
    assert not Multa.objects.filter(multa_origem_nic=multa).exists()
    assert usuario_logado.get("/multas/").status_code == 200


def test_nic_sem_valor_continua_aceito(usuario_logado, veiculo):
    (multa,) = _multas(veiculo, 1)
    usuario_logado.post(f"/multas/{multa.pk}/nic/", {"valor": "  "})
    assert multa.multa_nic.valor is None


def test_gerar_nd_com_cliente_invalido_e_404(usuario_logado, db):
    assert usuario_logado.get("/multas/gerar-nd/?cliente=abc").status_code == 404
    assert usuario_logado.get("/multas/gerar-nd/?cliente=999").status_code == 404
