"""Etapa 17 (achados extras) — frente km-manutenção: registro de manutenção."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.frota.models import Veiculo
from apps.manutencao.models import Manutencao
from apps.pessoas.models import Cliente

HOJE = timezone.localdate()


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(
        placa="EXT1A17",
        marca_modelo="Onix",
        km_atual=40_000,
        data_aquisicao=HOJE - timedelta(days=300),
    )


def _dados(**extra):
    dados = {
        "data": HOJE.isoformat(),
        "km": "40000",
        "descricao": "Troca da embreagem",
        "origem_custo": "particular",
        "responsavel": "empresa",
        "pagamento_custo": "pendente",
    }
    dados.update(extra)
    return dados


def _url(veiculo):
    return f"/manutencao/registrar/{veiculo.pk}/"


def _erros(resposta):
    return resposta.context["form"].errors


# --- carro vendido -----------------------------------------------------------


def _vender(veiculo, dias_atras=10):
    veiculo.status = Veiculo.Status.VENDIDO
    veiculo.data_venda = HOJE - timedelta(days=dias_atras)
    veiculo.save()


def test_carro_vendido_nao_aceita_manutencao_nova(usuario_logado, veiculo):
    _vender(veiculo)
    resposta = usuario_logado.post(
        _url(veiculo), _dados(data_entrada=HOJE.isoformat(), custo_real="300,00")
    )
    assert resposta.status_code == 200
    assert "já foi vendido" in _erros(resposta)["data"][0]
    assert not Manutencao.objects.exists()


def test_carro_vendido_aceita_gasto_de_antes_da_venda(usuario_logado, veiculo):
    _vender(veiculo)
    antes = (HOJE - timedelta(days=20)).isoformat()
    resposta = usuario_logado.post(_url(veiculo), _dados(data=antes, custo_real="300,00"))
    assert resposta.status_code == 302
    assert Manutencao.objects.count() == 1


def test_carro_vendido_nao_fica_na_oficina(usuario_logado, veiculo):
    _vender(veiculo)
    antes = (HOJE - timedelta(days=20)).isoformat()
    resposta = usuario_logado.post(_url(veiculo), _dados(data=antes, data_entrada=antes))
    assert resposta.status_code == 200
    assert "data_saida" in _erros(resposta)
    assert not Manutencao.objects.exists()


def test_detalhe_do_carro_vendido_sem_botao_de_manutencao(usuario_logado, veiculo):
    _vender(veiculo)
    resposta = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/")
    assert resposta.status_code == 200
    html = resposta.content.decode()
    assert f"/manutencao/registrar/{veiculo.pk}/" not in html
    aviso = usuario_logado.get(_url(veiculo)).content.decode()
    assert "já foi vendido" in aviso


# --- "Já pagou a oficina? Sim" sem valor -------------------------------------


def test_registrar_pago_sem_custo_e_recusado(usuario_logado, veiculo):
    resposta = usuario_logado.post(_url(veiculo), _dados(pagamento_custo="pago"))
    assert resposta.status_code == 200
    assert "Informe quanto pagou" in _erros(resposta)["custo_real"][0]
    # zero (a proteção cobriu) vale
    resposta = usuario_logado.post(_url(veiculo), _dados(pagamento_custo="pago", custo_real="0"))
    assert resposta.status_code == 302


def test_voltou_pago_sem_custo_e_recusado(usuario_logado, veiculo):
    manutencao = Manutencao.objects.create(
        veiculo=veiculo,
        tipo="corretiva",
        data=HOJE - timedelta(days=7),
        data_entrada=HOJE - timedelta(days=7),
        descricao="Motor",
    )
    url = f"/manutencao/voltou/{manutencao.pk}/"
    dados = {"data_saida": HOJE.isoformat(), "pagamento_custo": "pago"}
    resposta = usuario_logado.post(url, dados)
    assert resposta.status_code == 200
    assert "Informe quanto pagou" in _erros(resposta)["custo_real"][0]
    manutencao.refresh_from_db()
    assert manutencao.data_saida is None
    assert usuario_logado.post(url, {**dados, "custo_real": "250,00"}).status_code == 302


# --- data no futuro e antes da compra ----------------------------------------


def test_data_no_futuro_e_recusada(usuario_logado, veiculo):
    amanha = (HOJE + timedelta(days=1)).isoformat()
    resposta = usuario_logado.post(_url(veiculo), _dados(data=amanha))
    assert resposta.status_code == 200
    assert "depois de hoje" in _erros(resposta)["data"][0]
    assert not Manutencao.objects.exists()


def test_gasto_antes_da_compra_e_recusado(usuario_logado, veiculo):
    antes = (veiculo.data_aquisicao - timedelta(days=30)).isoformat()
    resposta = usuario_logado.post(_url(veiculo), _dados(data=antes, custo_real="300,00"))
    assert resposta.status_code == 200
    assert "foi comprado em" in _erros(resposta)["data"][0]


def test_ultima_troca_antes_da_compra_sem_gasto_vale(usuario_logado, veiculo):
    # "Informar a última troca": feita pelo dono anterior, sem custo para a empresa
    antes = (veiculo.data_aquisicao - timedelta(days=30)).isoformat()
    resposta = usuario_logado.post(_url(veiculo), _dados(data=antes, km="35000", confirmar_km="on"))
    assert resposta.status_code == 302


# --- "Já gerar a cobrança" sem cliente no carro ------------------------------


def test_gerar_cobranca_sem_cliente_nao_salva(usuario_logado, veiculo):
    resposta = usuario_logado.post(
        _url(veiculo),
        _dados(responsavel="cliente", valor_cobrado_cliente="100,00", gerar_cobranca="on"),
    )
    assert resposta.status_code == 200
    assert "Ninguém estava com o carro" in _erros(resposta)["gerar_cobranca"][0]
    assert not Manutencao.objects.exists()


def test_gerar_cobranca_com_cliente_salva_e_cobra(usuario_logado, veiculo):
    cliente = Cliente.objects.create(nome="Lucas", cpf_cnpj="52998224725")
    Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=10),
        km_entrega=39_000,
        valor_semanal=Decimal("650.00"),
    )
    resposta = usuario_logado.post(
        _url(veiculo),
        _dados(responsavel="cliente", valor_cobrado_cliente="100,00", gerar_cobranca="on"),
    )
    assert resposta.status_code == 302
    assert Manutencao.objects.get().cobranca_repasse.cliente == cliente


# --- título da aba -----------------------------------------------------------


def test_titulo_da_aba_segue_o_padrao(usuario_logado, veiculo):
    html = usuario_logado.get(_url(veiculo)).content.decode()
    assert f"<title>Registrar manutenção — {veiculo.placa} — Trade Rent a Car</title>" in html
