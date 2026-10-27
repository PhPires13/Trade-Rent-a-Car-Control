"""Qualidade final da manutenção: KM corrigido, KM menor, apagar, tipo automático e textos."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib import admin
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.financeiro.models import Cobranca
from apps.frota.models import Veiculo
from apps.manutencao.models import ItemPreventiva, Manutencao
from apps.manutencao.repasse import gerar_repasse
from apps.manutencao.services import texto_faltam_km
from apps.manutencao.views import VoltouDaOficinaForm, suspeita_do_km_da_manutencao
from apps.pessoas.models import Cliente

HOJE = timezone.localdate()


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QLF1A23", marca_modelo="Onix", km_atual=50_000)


@pytest.fixture
def oleo(db):
    return ItemPreventiva.objects.get(nome="Troca de óleo e filtro")


@pytest.fixture
def suspensao(db):
    return ItemPreventiva.objects.get(nome="Suspensão")


def _dados(**extra):
    dados = {
        "data": HOJE.isoformat(),
        "km": "50000",
        "descricao": "Serviço",
        "origem_custo": "particular",
        "responsavel": "empresa",
        "pagamento_custo": "pendente",
    }
    dados.update(extra)
    return dados


def _manutencao(veiculo, **extra):
    dados = {"veiculo": veiculo, "tipo": "corretiva", "data": HOJE, "descricao": "Freio"}
    dados.update(extra)
    return Manutencao.objects.create(**dados)


# ---------- KM corrigido na edição ----------


def test_corrigir_km_para_menos_baixa_o_km_atual(usuario_logado, veiculo):
    manutencao = _manutencao(veiculo, km=500_000)
    veiculo.refresh_from_db()
    assert veiculo.km_atual == 500_000
    resposta = usuario_logado.post(
        f"/manutencao/editar/{manutencao.pk}/", _dados(km="50000"), follow=True
    )
    veiculo.refresh_from_db()
    assert veiculo.km_atual == 50_000
    assert "voltou para 50.000 km" in resposta.content.decode()


def test_corrigir_km_para_mais_continua_subindo(usuario_logado, veiculo):
    manutencao = _manutencao(veiculo, km=50_000)
    usuario_logado.post(f"/manutencao/editar/{manutencao.pk}/", _dados(km="51000"))
    veiculo.refresh_from_db()
    assert veiculo.km_atual == 51_000


# ---------- KM menor que o atual ----------


def test_km_menor_que_o_atual_avisa(veiculo):
    _manutencao(veiculo, km=50_000, data=HOJE - timedelta(days=3))
    veiculo.refresh_from_db()
    aviso = suspeita_do_km_da_manutencao(veiculo, 5_000, HOJE)
    assert "Você digitou 5.000 km, mas o carro já marcou 50.000 km" in aviso
    assert "Confirmo o KM" in aviso


def test_manutencao_retroativa_com_km_menor_nao_avisa(veiculo):
    _manutencao(veiculo, km=50_000, data=HOJE - timedelta(days=3))
    veiculo.refresh_from_db()
    assert suspeita_do_km_da_manutencao(veiculo, 40_000, HOJE - timedelta(days=60)) is None


def test_tela_exige_confirmar_km_menor(usuario_logado, veiculo):
    _manutencao(veiculo, km=50_000, data=HOJE - timedelta(days=3))
    url = f"/manutencao/registrar/{veiculo.pk}/"
    resposta = usuario_logado.post(url, _dados(km="5000", token="a1"))
    assert resposta.status_code == 200
    assert "já marcou 50.000 km" in resposta.content.decode()
    assert veiculo.manutencoes.count() == 1
    usuario_logado.post(url, _dados(km="5000", token="a2", confirmar_km="on"))
    assert veiculo.manutencoes.count() == 2


def test_corrigir_a_propria_manutencao_que_marcou_o_km_nao_avisa(usuario_logado, veiculo):
    manutencao = _manutencao(veiculo, km=500_000)
    veiculo.refresh_from_db()
    assert suspeita_do_km_da_manutencao(veiculo, 50_000, HOJE, manutencao) is None


# ---------- Apagar ----------


def test_apagar_manutencao_refaz_km_e_status(usuario_logado, veiculo):
    manutencao = _manutencao(veiculo, km=500_000, data_entrada=HOJE)
    veiculo.refresh_from_db()
    veiculo.status = Veiculo.Status.EM_MANUTENCAO
    veiculo.save(update_fields=["status"])
    resposta = usuario_logado.post(f"/manutencao/apagar/{manutencao.pk}/", follow=True)
    assert not Manutencao.objects.filter(pk=manutencao.pk).exists()
    veiculo.refresh_from_db()
    assert veiculo.km_atual == 50_000  # o KM do cadastro antes do número errado
    assert veiculo.status == Veiculo.Status.DISPONIVEL
    html = resposta.content.decode()
    assert "apagada" in html
    assert "voltou a ficar disponível" in html


def test_apagar_manutencao_fechada_nao_mexe_no_status_manual(usuario_logado, veiculo):
    manutencao = _manutencao(veiculo, km=50_000)
    veiculo.status = Veiculo.Status.EM_MANUTENCAO
    veiculo.save(update_fields=["status"])
    usuario_logado.post(f"/manutencao/apagar/{manutencao.pk}/")
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.EM_MANUTENCAO


def test_apagar_bloqueado_com_cobranca_de_repasse(usuario_logado, veiculo):
    cliente = Cliente.objects.create(nome="Bruna", cpf_cnpj="222.333.444-55")
    Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=10),
        valor_semanal=Decimal("650.00"),
        km_entrega=veiculo.km_atual,
    )
    manutencao = _manutencao(veiculo, responsavel="cliente", valor_cobrado_cliente=Decimal("300"))
    gerar_repasse(manutencao)
    resposta = usuario_logado.post(f"/manutencao/apagar/{manutencao.pk}/", follow=True)
    assert Manutencao.objects.filter(pk=manutencao.pk).exists()
    assert "Cancele a cobrança no Financeiro" in resposta.content.decode()

    manutencao.refresh_from_db()
    Cobranca.objects.filter(pk=manutencao.cobranca_repasse_id).update(
        status=Cobranca.Status.CANCELADA
    )
    usuario_logado.post(f"/manutencao/apagar/{manutencao.pk}/")
    assert not Manutencao.objects.filter(pk=manutencao.pk).exists()


def test_apagar_so_por_post(usuario_logado, veiculo):
    manutencao = _manutencao(veiculo)
    assert usuario_logado.get(f"/manutencao/apagar/{manutencao.pk}/").status_code == 405
    assert Manutencao.objects.filter(pk=manutencao.pk).exists()


def test_tela_de_editar_tem_botao_apagar(usuario_logado, veiculo):
    manutencao = _manutencao(veiculo)
    html = usuario_logado.get(f"/manutencao/editar/{manutencao.pk}/").content.decode()
    assert f"/manutencao/apagar/{manutencao.pk}/" in html


# ---------- Tipo automático ----------


@pytest.mark.parametrize(
    ("item_nome", "esperado"),
    [("Suspensão", "esporadica"), ("Troca de óleo e filtro", "preventiva"), (None, "corretiva")],
)
def test_tipo_sai_do_item(usuario_logado, veiculo, item_nome, esperado):
    extra = {"tipo": "corretiva"}  # tipo enviado à mão é ignorado
    if item_nome:
        extra["item"] = ItemPreventiva.objects.get(nome=item_nome).pk
    resposta = usuario_logado.post(f"/manutencao/registrar/{veiculo.pk}/", _dados(**extra))
    assert resposta.status_code == 302
    assert veiculo.manutencoes.get().tipo == esperado


def test_formulario_nao_tem_campo_tipo(usuario_logado, veiculo):
    form = usuario_logado.get(f"/manutencao/registrar/{veiculo.pk}/").context["form"]
    assert "tipo" not in form.fields


# ---------- Escolher o carro / data de hoje ----------


def test_preventivas_tem_seletor_com_todos_os_carros(usuario_logado, veiculo):
    vendido = Veiculo.objects.create(
        placa="VND1A23", marca_modelo="Gol", km_atual=1, status=Veiculo.Status.VENDIDO
    )
    html = usuario_logado.get("/manutencao/preventivas/").content.decode()
    assert "Qual carro?" in html or "qual carro?" in html
    assert f'<option value="{veiculo.pk}">QLF1A23' in html
    assert f'value="{vendido.pk}"' not in html


def test_escolher_carro_leva_ao_registro(usuario_logado, veiculo):
    resposta = usuario_logado.get(f"/manutencao/registrar/?veiculo={veiculo.pk}")
    assert resposta["Location"] == f"/manutencao/registrar/{veiculo.pk}/"
    torto = usuario_logado.get("/manutencao/registrar/?veiculo=abc")
    assert torto["Location"] == "/manutencao/preventivas/"


def test_registrar_sugere_a_data_de_hoje(usuario_logado, veiculo):
    form = usuario_logado.get(f"/manutencao/registrar/{veiculo.pk}/").context["form"]
    assert form.initial["data"] == HOJE


# ---------- Textos ----------


def test_texto_de_quanto_falta():
    assert texto_faltam_km(0) == "vence agora"
    assert texto_faltam_km(-1200) == "vencida há 1.200 km"
    assert texto_faltam_km(800) == "faltam 800 km"
    assert texto_faltam_km(None) is None


def test_textos_simples():
    descricao = Manutencao._meta.get_field("descricao").help_text
    assert "§" not in descricao and "desmobilização" not in descricao
    assert "vender o carro" in descricao
    assert VoltouDaOficinaForm().fields["custo_real"].label == "Quanto a oficina cobrou (R$)"


def test_historico_usa_o_mesmo_nome_do_custo(usuario_logado, veiculo):
    _manutencao(veiculo, custo_real=Decimal("100"))
    html = usuario_logado.get(f"/manutencao/historico/{veiculo.pk}/").content.decode()
    assert "Quanto a oficina cobrou" in html
    assert "Custo real" not in html
    assert "— Trade Rent a Car</title>" in html


# ---------- ?volta= torto ----------


def test_volta_sem_barra_nao_da_erro_500(usuario_logado, veiculo):
    manutencao = _manutencao(veiculo, data_entrada=HOJE)
    resposta = usuario_logado.post(
        f"/manutencao/voltou/{manutencao.pk}/?volta=historico",
        {"data_saida": HOJE.isoformat(), "custo_real": "0", "pagamento_custo": "pago"},
    )
    assert resposta.status_code == 302
    assert resposta["Location"] == f"/manutencao/historico/{veiculo.pk}/"
    outro = usuario_logado.get(f"/manutencao/voltou/{manutencao.pk}/?volta=//mal.com/x")
    assert "mal.com" not in outro["Location"]


# ---------- Admin ----------


def test_admin_nao_monta_select_de_cobrancas():
    manutencao_admin = admin.site._registry[Manutencao]
    assert "cobranca_repasse" in manutencao_admin.raw_id_fields
    assert "veiculo" in manutencao_admin.raw_id_fields


# ---------- Revisão: casos extras ----------


def test_so_com_km_do_cadastro_digito_faltando_avisa(veiculo):
    """Carro sem nenhum lançamento: 5.000 num carro cadastrado com 50.000 é dígito faltando."""
    aviso = suspeita_do_km_da_manutencao(veiculo, 5_000, HOJE)
    assert "Você digitou 5.000 km, mas o carro está cadastrado com 50.000 km" in aviso


def test_so_com_km_do_cadastro_ultima_troca_antes_do_sistema_nao_avisa(veiculo):
    """ "Informar a última troca" feita antes de o carro entrar no sistema é normal."""
    assert suspeita_do_km_da_manutencao(veiculo, 45_000, HOJE - timedelta(days=40)) is None


def test_editar_manutencao_antiga_acerta_o_tipo_pelo_item(usuario_logado, veiculo, suspensao):
    """Suspensão gravada como corretiva passa a contar como esporádica no 'Quando vender'."""
    manutencao = _manutencao(veiculo, km=50_000, item=suspensao, tipo="corretiva")
    usuario_logado.post(
        f"/manutencao/editar/{manutencao.pk}/", _dados(item=str(suspensao.pk), tipo="preventiva")
    )
    manutencao.refresh_from_db()
    assert manutencao.tipo == Manutencao.Tipo.ESPORADICA


def test_apagar_com_cobranca_cancelada_pode(usuario_logado, veiculo):
    cliente = Cliente.objects.create(nome="Motorista QF", cpf_cnpj="529.982.247-25")
    cobranca = Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.REPASSE_MANUTENCAO,
        descricao="Conserto",
        valor=Decimal("300.00"),
        vencimento=HOJE,
        status=Cobranca.Status.CANCELADA,
    )
    manutencao = _manutencao(veiculo, km=50_000, cobranca_repasse=cobranca)
    usuario_logado.post(f"/manutencao/apagar/{manutencao.pk}/")
    assert not Manutencao.objects.filter(pk=manutencao.pk).exists()
    assert Cobranca.objects.filter(pk=cobranca.pk).exists()


def test_ano_torto_na_manutencao_nao_da_erro_500(usuario_logado, veiculo):
    resposta = usuario_logado.post(
        f"/manutencao/registrar/{veiculo.pk}/", _dados(data="0001-01-01", token="ano1")
    )
    assert resposta.status_code == 200
    assert not veiculo.manutencoes.exists()


def test_apagar_volta_para_o_historico(usuario_logado, veiculo):
    manutencao = _manutencao(veiculo, km=50_000)
    resposta = usuario_logado.post(f"/manutencao/apagar/{manutencao.pk}/")
    assert resposta.status_code == 302
    assert resposta.url == f"/manutencao/historico/{veiculo.pk}/"
