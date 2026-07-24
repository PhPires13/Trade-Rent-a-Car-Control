from datetime import date

import pytest

from apps.frota.models import Veiculo
from apps.manutencao.models import IntervaloPersonalizado, ItemPreventiva, Manutencao
from apps.manutencao.services import StatusPreventiva, preventivas_em_alerta, resumo_preventivas


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_atual=95_500)


@pytest.fixture
def oleo(db):
    return ItemPreventiva.objects.get(nome="Troca de óleo e filtro")


def test_seed_do_plano_confirmado_pelos_donos(db):
    assert ItemPreventiva.objects.get(nome="Troca de óleo e filtro").intervalo_km_padrao == 10_000
    assert ItemPreventiva.objects.get(nome="Alinhamento").intervalo_km_padrao == 10_000
    assert (
        ItemPreventiva.objects.get(nome="Kit correia dentada + óleo da caixa").intervalo_km_padrao
        == 60_000
    )
    assert ItemPreventiva.objects.get(nome="Pneus (2 unidades)").intervalo_km_padrao == 30_000
    assert ItemPreventiva.objects.get(nome="Suspensão").intervalo_km_padrao is None


def _status_do_item(veiculo, item):
    return next(p for p in resumo_preventivas(veiculo) if p.item == item)


def test_sem_execucao_fica_sem_registro(veiculo, oleo):
    assert _status_do_item(veiculo, oleo).status == StatusPreventiva.SEM_REGISTRO


def test_ciclo_ok_proxima_e_vencida(veiculo, oleo):
    Manutencao.objects.create(
        veiculo=veiculo,
        item=oleo,
        tipo="preventiva",
        data=date(2026, 5, 1),
        km=90_000,
        descricao="Troca de óleo",
    )
    # km_atual 95_500 → próxima aos 100_000, faltam 4_500 → OK
    assert _status_do_item(veiculo, oleo).status == StatusPreventiva.OK

    veiculo.km_atual = 99_200  # faltam 800 → PROXIMA
    veiculo.save()
    assert _status_do_item(veiculo, oleo).status == StatusPreventiva.PROXIMA

    veiculo.km_atual = 100_500  # passou → VENCIDA
    veiculo.save()
    p = _status_do_item(veiculo, oleo)
    assert p.status == StatusPreventiva.VENCIDA
    assert p.faltam_km == -500


def test_nova_execucao_zera_o_ciclo(veiculo, oleo):
    Manutencao.objects.create(
        veiculo=veiculo,
        item=oleo,
        tipo="preventiva",
        data=date(2026, 5, 1),
        km=90_000,
        descricao="Troca de óleo",
    )
    veiculo.km_atual = 100_500
    veiculo.save()
    Manutencao.objects.create(
        veiculo=veiculo,
        item=oleo,
        tipo="preventiva",
        data=date(2026, 7, 19),
        km=100_500,
        descricao="Troca de óleo",
    )
    p = _status_do_item(veiculo, oleo)
    assert p.km_proximo == 110_500
    assert p.status == StatusPreventiva.OK


def test_intervalo_personalizado_por_veiculo(veiculo, db):
    pneus = ItemPreventiva.objects.get(nome="Pneus (2 unidades)")
    IntervaloPersonalizado.objects.create(veiculo=veiculo, item=pneus, intervalo_km=20_000)
    Manutencao.objects.create(
        veiculo=veiculo,
        item=pneus,
        tipo="preventiva",
        data=date(2026, 1, 1),
        km=80_000,
        descricao="Pneus dianteiros",
    )
    p = _status_do_item(veiculo, pneus)
    assert p.intervalo_km == 20_000
    assert p.km_proximo == 100_000


def test_manutencao_com_km_maior_atualiza_veiculo(veiculo, oleo):
    Manutencao.objects.create(
        veiculo=veiculo,
        item=oleo,
        tipo="preventiva",
        data=date(2026, 7, 19),
        km=96_000,
        descricao="Troca de óleo",
    )
    veiculo.refresh_from_db()
    assert veiculo.km_atual == 96_000


def test_alertas_da_frota_ignoram_fora_de_locacao(veiculo, oleo, db):
    pessoal = Veiculo.objects.create(
        placa="RVZ9J95", marca_modelo="HB20", uso=Veiculo.Uso.FORA_LOCACAO, km_atual=120_000
    )
    for carro in (veiculo, pessoal):
        Manutencao.objects.create(
            veiculo=carro,
            item=oleo,
            tipo="preventiva",
            data=date(2026, 1, 1),
            km=carro.km_atual - 11_000,
            descricao="Troca de óleo",
        )
    alertas = preventivas_em_alerta()
    assert [v.placa for v, _ in alertas] == ["QXQ6C10"]


@pytest.fixture
def usuario_logado(client, django_user_model):
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    client.login(username="dono", password="senha-forte-123")
    return client


def test_telas_de_manutencao_renderizam(usuario_logado, veiculo, oleo):
    Manutencao.objects.create(
        veiculo=veiculo,
        item=oleo,
        tipo="preventiva",
        data=date(2026, 5, 1),
        km=90_000,
        descricao="Troca de óleo",
    )
    resposta = usuario_logado.get("/manutencao/preventivas/")
    assert resposta.status_code == 200
    # o status calculado precisa aparecer renderizado (regressão: classe chamada pelo template)
    assert "Em dia" in resposta.content.decode()
    assert usuario_logado.get(f"/manutencao/registrar/{veiculo.pk}/").status_code == 200
    assert usuario_logado.get(f"/manutencao/historico/{veiculo.pk}/").status_code == 200


def test_registrar_manutencao_pela_tela(usuario_logado, veiculo, oleo):
    resposta = usuario_logado.post(
        f"/manutencao/registrar/{veiculo.pk}/",
        {
            "item": oleo.pk,
            "tipo": "preventiva",
            "data": "2026-07-19",
            "origem_custo": "particular",
            "responsavel": "empresa",
            "pagamento_custo": "pendente",
            "km": "96000",
            "descricao": "Troca de óleo e filtro na By Car",
        },
    )
    assert resposta.status_code == 302
    assert veiculo.manutencoes.count() == 1
