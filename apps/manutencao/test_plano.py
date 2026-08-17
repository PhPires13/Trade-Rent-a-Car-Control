from datetime import date

import pytest

from apps.frota.models import Veiculo
from apps.manutencao.models import IntervaloPersonalizado, ItemPreventiva, Manutencao
from apps.manutencao.services import resumo_preventivas


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_atual=50_000)


def nomes_do_resumo(veiculo):
    return [p.item.nome for p in resumo_preventivas(veiculo)]


def test_tela_do_plano_abre(usuario_logado, veiculo):
    resposta = usuario_logado.get("/manutencao/plano/")
    assert resposta.status_code == 200
    assert "Troca de óleo e filtro" in resposta.content.decode()


def test_novo_item_aparece_no_resumo_de_preventivas(usuario_logado, veiculo):
    assert "Filtro de ar" not in nomes_do_resumo(veiculo)
    resposta = usuario_logado.post(
        "/manutencao/plano/",
        {
            "acao": "novo_item",
            "nome": "Filtro de ar",
            "intervalo_km_padrao": "20000",
            "ativo": "on",
        },
    )
    assert resposta.status_code == 302
    item = ItemPreventiva.objects.get(nome="Filtro de ar")
    assert item.intervalo_km_padrao == 20_000
    assert "Filtro de ar" in nomes_do_resumo(veiculo)


def test_item_sem_intervalo_e_esporadico_e_fica_fora_do_resumo(usuario_logado, veiculo):
    usuario_logado.post(
        "/manutencao/plano/",
        {"acao": "novo_item", "nome": "Ar-condicionado", "intervalo_km_padrao": "", "ativo": "on"},
    )
    item = ItemPreventiva.objects.get(nome="Ar-condicionado")
    assert item.intervalo_km_padrao is None
    assert "Ar-condicionado" not in nomes_do_resumo(veiculo)


def test_editar_intervalo_do_item_reflete_no_calculo(usuario_logado, veiculo):
    item = ItemPreventiva.objects.get(nome="Troca de óleo e filtro")
    Manutencao.objects.create(
        veiculo=veiculo,
        item=item,
        tipo="preventiva",
        data=date(2026, 7, 1),
        km=50_000,
        descricao="Óleo",
    )
    antes = [p for p in resumo_preventivas(veiculo) if p.item == item][0]
    assert antes.km_proximo == 60_000

    resposta = usuario_logado.post(
        "/manutencao/plano/",
        {
            "acao": "editar_item",
            "item_id": item.pk,
            "nome": item.nome,
            "intervalo_km_padrao": "15000",
            "ativo": "on",
        },
    )
    assert resposta.status_code == 302
    item.refresh_from_db()
    assert item.intervalo_km_padrao == 15_000
    depois = [p for p in resumo_preventivas(veiculo) if p.item == item][0]
    assert depois.km_proximo == 65_000


def test_desativar_item_tira_do_resumo(usuario_logado, veiculo):
    item = ItemPreventiva.objects.get(nome="Alinhamento")
    usuario_logado.post(
        "/manutencao/plano/",
        {
            "acao": "editar_item",
            "item_id": item.pk,
            "nome": item.nome,
            "intervalo_km_padrao": item.intervalo_km_padrao,
        },
    )
    item.refresh_from_db()
    assert item.ativo is False
    assert "Alinhamento" not in nomes_do_resumo(veiculo)


def test_intervalo_personalizado_criado_e_removido_pela_tela(usuario_logado, veiculo):
    pneus = ItemPreventiva.objects.get(nome="Pneus (2 unidades)")
    assert pneus.intervalo_km_padrao == 30_000
    Manutencao.objects.create(
        veiculo=veiculo,
        item=pneus,
        tipo="preventiva",
        data=date(2026, 7, 1),
        km=50_000,
        descricao="Pneus",
    )

    resposta = usuario_logado.post(
        "/manutencao/plano/",
        {
            "acao": "novo_intervalo",
            "veiculo": veiculo.pk,
            "item": pneus.pk,
            "intervalo_km": "20000",
        },
    )
    assert resposta.status_code == 302
    personalizado = IntervaloPersonalizado.objects.get(veiculo=veiculo, item=pneus)
    assert personalizado.intervalo_km == 20_000
    status = [p for p in resumo_preventivas(veiculo) if p.item == pneus][0]
    assert status.intervalo_km == 20_000
    assert status.km_proximo == 70_000

    resposta = usuario_logado.post(
        "/manutencao/plano/", {"acao": "remover_intervalo", "intervalo_id": personalizado.pk}
    )
    assert resposta.status_code == 302
    assert not IntervaloPersonalizado.objects.filter(pk=personalizado.pk).exists()
    status = [p for p in resumo_preventivas(veiculo) if p.item == pneus][0]
    assert status.intervalo_km == 30_000


def test_intervalo_duplicado_para_o_mesmo_item_nao_cria_segundo(usuario_logado, veiculo):
    pneus = ItemPreventiva.objects.get(nome="Pneus (2 unidades)")
    dados = {
        "acao": "novo_intervalo",
        "veiculo": veiculo.pk,
        "item": pneus.pk,
        "intervalo_km": "20000",
    }
    usuario_logado.post("/manutencao/plano/", dados)
    resposta = usuario_logado.post("/manutencao/plano/", dict(dados, intervalo_km="25000"))
    assert resposta.status_code == 200
    assert IntervaloPersonalizado.objects.filter(veiculo=veiculo, item=pneus).count() == 1


def test_preventivas_linka_para_o_plano(usuario_logado, veiculo):
    conteudo = usuario_logado.get("/manutencao/preventivas/").content.decode()
    assert "/manutencao/plano/" in conteudo
