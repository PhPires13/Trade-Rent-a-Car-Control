"""Revisão da frente manutenção: valores negativos, datas futuras, parâmetros inválidos."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from apps.frota.models import Veiculo
from apps.manutencao.models import ItemPreventiva, Manutencao

HOJE = timezone.localdate()


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="REV1A23", marca_modelo="Onix", km_atual=100_000)


@pytest.fixture
def oleo(db):
    return ItemPreventiva.objects.get(nome="Troca de óleo e filtro")  # 10.000 km


def _na_oficina(veiculo, dias=3, **extra):
    dados = {
        "veiculo": veiculo,
        "tipo": Manutencao.Tipo.CORRETIVA,
        "data": HOJE - timedelta(days=dias),
        "descricao": "Embreagem",
        "data_entrada": HOJE - timedelta(days=dias),
    }
    dados.update(extra)
    return Manutencao.objects.create(**dados)


def _dados_registro(**extra):
    dados = {
        "tipo": "corretiva",
        "data": HOJE.isoformat(),
        "km": "100000",
        "descricao": "Troca da embreagem",
        "origem_custo": "particular",
        "responsavel": "empresa",
        "pagamento_custo": "pendente",
    }
    dados.update(extra)
    return dados


def test_custo_negativo_e_recusado_no_registro(usuario_logado, veiculo):
    resposta = usuario_logado.post(
        f"/manutencao/registrar/{veiculo.pk}/", _dados_registro(custo_real="-500,00")
    )
    assert resposta.status_code == 200
    assert "não pode ser negativo" in resposta.content.decode()
    assert not Manutencao.objects.exists()


def test_valor_do_cliente_negativo_e_recusado(usuario_logado, veiculo):
    resposta = usuario_logado.post(
        f"/manutencao/registrar/{veiculo.pk}/",
        _dados_registro(responsavel="cliente", valor_cobrado_cliente="-10"),
    )
    assert resposta.status_code == 200
    assert not Manutencao.objects.exists()


def test_custo_final_negativo_e_recusado_no_voltou(usuario_logado, veiculo):
    manutencao = _na_oficina(veiculo)
    resposta = usuario_logado.post(
        f"/manutencao/voltou/{manutencao.pk}/",
        {"data_saida": HOJE.isoformat(), "custo_real": "-1", "pagamento_custo": "pago"},
    )
    assert resposta.status_code == 200
    manutencao.refresh_from_db()
    assert manutencao.data_saida is None


def test_entrada_e_saida_no_futuro_sao_recusadas(usuario_logado, veiculo):
    amanha = (HOJE + timedelta(days=1)).isoformat()
    resposta = usuario_logado.post(
        f"/manutencao/registrar/{veiculo.pk}/", _dados_registro(data_entrada=amanha)
    )
    assert "entrada na oficina não pode ser depois de hoje" in resposta.content.decode()
    resposta = usuario_logado.post(
        f"/manutencao/registrar/{veiculo.pk}/",
        _dados_registro(data_entrada=HOJE.isoformat(), data_saida=amanha),
    )
    assert "saída não pode ser depois de hoje" in resposta.content.decode()
    assert not Manutencao.objects.exists()


def test_voltou_sem_data_de_entrada_manda_para_editar(usuario_logado, veiculo):
    manutencao = Manutencao.objects.create(
        veiculo=veiculo, tipo="corretiva", data=HOJE, descricao="Lâmpada"
    )
    resposta = usuario_logado.post(
        f"/manutencao/voltou/{manutencao.pk}/",
        {"data_saida": HOJE.isoformat(), "pagamento_custo": "pago"},
    )
    assert resposta.status_code == 302
    assert resposta.url == f"/manutencao/editar/{manutencao.pk}/"
    manutencao.refresh_from_db()
    assert manutencao.data_saida is None


def test_voltou_repetido_nao_muda_a_saida(usuario_logado, veiculo):
    ontem = HOJE - timedelta(days=1)
    manutencao = _na_oficina(veiculo, data_saida=ontem)
    resposta = usuario_logado.post(
        f"/manutencao/voltou/{manutencao.pk}/",
        {"data_saida": HOJE.isoformat(), "custo_real": "99", "pagamento_custo": "pago"},
    )
    assert resposta.status_code == 302
    manutencao.refresh_from_db()
    assert manutencao.data_saida == ontem
    assert manutencao.custo_real is None


def test_informar_ultima_troca_nao_preenche_hoje_nem_km_atual(usuario_logado, veiculo, oleo):
    # Item nunca registrado: o link do quadro pede a última troca, que foi no passado.
    todas = usuario_logado.get("/manutencao/preventivas/?ver=todas").content.decode()
    assert f"?item={oleo.pk}&ultima=1" in todas

    resposta = usuario_logado.get(f"/manutencao/registrar/{veiculo.pk}/?item={oleo.pk}&ultima=1")
    inicial = resposta.context["form"].initial
    assert "data" not in inicial and "km" not in inicial
    assert inicial["item"] == oleo.pk
    assert "Informe quando foi a última troca" in resposta.content.decode()

    # o "Registrar feita" normal continua vindo com hoje e o KM atual
    normal = usuario_logado.get(f"/manutencao/registrar/{veiculo.pk}/?item={oleo.pk}")
    assert normal.context["form"].initial["km"] == 100_000


def test_preventiva_no_km_exato_diz_vence_agora(usuario_logado, veiculo, oleo):
    Manutencao.objects.create(
        veiculo=veiculo, item=oleo, tipo="preventiva", data=HOJE, km=90_000, descricao="Troca"
    )
    html = usuario_logado.get("/manutencao/preventivas/").content.decode()
    assert "vence agora" in html
    assert "vencida há 0 km" not in html


def test_historico_nao_diz_falta_pagar_sem_custo(usuario_logado, veiculo):
    Manutencao.objects.create(
        veiculo=veiculo,
        tipo="corretiva",
        data=HOJE,
        descricao="Coberto pela proteção",
        custo_real=Decimal("0"),
    )
    html = usuario_logado.get(f"/manutencao/historico/{veiculo.pk}/").content.decode()
    assert "Falta pagar" not in html


@pytest.mark.parametrize(
    "consulta", ["?item=abc&sinistro=xyz", "?item=999999&sinistro=999999", "?volta=//mal.com"]
)
def test_registrar_ignora_parametros_invalidos(usuario_logado, veiculo, consulta):
    resposta = usuario_logado.get(f"/manutencao/registrar/{veiculo.pk}/{consulta}")
    assert resposta.status_code == 200
    assert "mal.com" not in resposta.content.decode()


@pytest.mark.parametrize("rota", ["registrar/{v}/", "editar/{m}/", "voltou/{m}/"])
def test_telas_novas_exigem_login(db, veiculo, rota):
    manutencao = _na_oficina(veiculo)
    resposta = Client().get("/manutencao/" + rota.format(v=veiculo.pk, m=manutencao.pk))
    assert resposta.status_code == 302
    assert resposta.url.startswith("/entrar/")


def test_editar_e_voltou_inexistentes_dao_404(usuario_logado, db):
    assert usuario_logado.get("/manutencao/editar/999999/").status_code == 404
    assert usuario_logado.get("/manutencao/voltou/999999/").status_code == 404
