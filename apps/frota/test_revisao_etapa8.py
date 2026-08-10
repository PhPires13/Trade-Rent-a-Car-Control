"""Regressões dos achados confirmados na revisão adversarial da etapa 8."""

from datetime import date
from decimal import Decimal

import pytest

from apps.alocacoes.models import Alocacao
from apps.frota.models import Veiculo
from apps.manutencao.models import ItemPreventiva, Manutencao
from apps.pessoas.models import Cliente


@pytest.fixture
def usuario_logado(client, django_user_model):
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    client.login(username="dono", password="senha-forte-123")
    return client


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_atual=95_000)


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


def _dados_edicao(veiculo, **extras):
    dados = {
        "placa": veiculo.placa,
        "marca_modelo": veiculo.marca_modelo,
        "renavam": "",
        "chassi": "",
        "ano": "",
        "uso": veiculo.uso,
        "chave_reserva": veiculo.chave_reserva,
        "km_atual": veiculo.km_atual,
        "observacoes": "",
    }
    dados.update(extras)
    return dados


def test_status_nao_e_editavel_pelo_formulario(usuario_logado, veiculo, cliente):
    """O status é gerido pelos fluxos — editar por form dessincronizava a alocação."""
    Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=95_000,
    )
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.ALOCADO
    resposta = usuario_logado.post(
        f"/frota/veiculo/{veiculo.pk}/editar/",
        _dados_edicao(veiculo, status="disponivel"),  # campo extra é ignorado
    )
    assert resposta.status_code == 302
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.ALOCADO  # intacto


def test_km_atual_nao_pode_diminuir_na_edicao(usuario_logado, veiculo):
    resposta = usuario_logado.post(
        f"/frota/veiculo/{veiculo.pk}/editar/",
        _dados_edicao(veiculo, km_atual=9_500),
    )
    assert resposta.status_code == 200  # volta com erro no form
    assert "não diminui" in resposta.content.decode()
    veiculo.refresh_from_db()
    assert veiculo.km_atual == 95_000


def test_hub_ignora_preventiva_de_veiculo_inativo(usuario_logado, veiculo, db):
    """Mesma regra do painel: inativos fora dos alertas de preventiva."""
    oleo = ItemPreventiva.objects.get(nome="Troca de óleo e filtro")
    Manutencao.objects.create(
        veiculo=veiculo,
        item=oleo,
        tipo="preventiva",
        data=date(2026, 1, 1),
        km=80_000,
        descricao="Troca de óleo",  # vencida há 5.000 km
    )
    Veiculo.objects.filter(pk=veiculo.pk).update(status=Veiculo.Status.INATIVO)
    resposta = usuario_logado.get("/frota/?status=inativo")
    assert "preventiva em alerta" not in resposta.content.decode()


def test_hub_roda_com_queries_fixas(usuario_logado, cliente, django_assert_max_num_queries, db):
    """O nº de queries do hub não cresce com a frota (revisão etapa 8)."""
    oleo = ItemPreventiva.objects.get(nome="Troca de óleo e filtro")
    for indice in range(12):
        veiculo = Veiculo.objects.create(
            placa=f"TQ{indice:02d}A{indice:02d}", marca_modelo="Gol", km_atual=90_000
        )
        Manutencao.objects.create(
            veiculo=veiculo,
            item=oleo,
            tipo="preventiva",
            data=date(2026, 1, 1),
            km=80_000,
            descricao="Óleo",
        )
    with django_assert_max_num_queries(12):
        resposta = usuario_logado.get("/frota/")
    assert resposta.status_code == 200


def test_post_com_id_invalido_vira_404(usuario_logado, db):
    resposta = usuario_logado.post(
        "/frota/categorias/",
        {"categoria_id": "abc", "nome": "X", "valor_semanal_referencia": "1"},
    )
    assert resposta.status_code == 404
