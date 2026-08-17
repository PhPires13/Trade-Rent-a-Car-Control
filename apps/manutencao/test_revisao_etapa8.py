"""Regressões dos achados da revisão da etapa 8 no plano de manutenção."""

import pytest

from apps.frota.models import Veiculo
from apps.manutencao.models import IntervaloPersonalizado, ItemPreventiva


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_atual=90_000)


def test_item_com_personalizados_nao_vira_esporadico(usuario_logado, veiculo):
    """Zerar o padrão apagaria o alerta e deixaria o personalizado órfão."""
    pneus = ItemPreventiva.objects.get(nome="Pneus (2 unidades)")
    IntervaloPersonalizado.objects.create(veiculo=veiculo, item=pneus, intervalo_km=20_000)
    usuario_logado.post(
        "/manutencao/plano/",
        {
            "acao": "editar_item",
            "item_id": pneus.pk,
            "nome": pneus.nome,
            "intervalo_km_padrao": "",
            "ativo": "on",
        },
    )
    pneus.refresh_from_db()
    assert pneus.intervalo_km_padrao == 30_000  # intacto


def test_intervalo_personalizado_zero_e_rejeitado(usuario_logado, veiculo):
    """0 é falsy: seria exibido na tabela mas ignorado no cálculo."""
    oleo = ItemPreventiva.objects.get(nome="Troca de óleo e filtro")
    usuario_logado.post(
        "/manutencao/plano/",
        {
            "acao": "novo_intervalo",
            "veiculo": veiculo.pk,
            "item": oleo.pk,
            "intervalo_km": "0",
        },
    )
    assert not IntervaloPersonalizado.objects.filter(veiculo=veiculo).exists()


def test_post_com_id_invalido_vira_404(usuario_logado, db):
    resposta = usuario_logado.post(
        "/manutencao/plano/",
        {"acao": "remover_intervalo", "intervalo_id": "abc"},
    )
    assert resposta.status_code == 404
