"""Pendências da etapa 15 na frente frota: KM esquecido na ficha, link "Informar
última troca", C14 no Admin, mensagem do KM menor e tela de venda."""

from datetime import date
from decimal import Decimal
from urllib.parse import quote

import pytest

from apps.alocacoes.models import Alocacao
from apps.frota.admin import VeiculoAdminForm
from apps.frota.alertas import pendencias_do_veiculo
from apps.frota.models import Veiculo
from apps.frota.views import VeiculoForm
from apps.km.models import RegistroKm
from apps.manutencao.models import ItemPreventiva
from apps.pessoas.models import Cliente

HOJE = date(2026, 10, 20)


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(
        placa="QXQ6C10",
        marca_modelo="Gol",
        km_atual=95_000,
        valor_venda_estimado=Decimal("45000.00"),
    )


@pytest.fixture
def alocacao(veiculo):
    cliente = Cliente.objects.create(nome="Ana", cpf_cnpj="111.222.333-44")
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=95_000,
    )


def _leitura(veiculo, mes):
    RegistroKm.objects.create(veiculo=veiculo, mes_referencia=mes, data_leitura=mes, km=95_000)


def _km(veiculo):
    return [p for p in pendencias_do_veiculo(veiculo, hoje=HOJE) if p["texto"].startswith("KM")]


# --- KM pendente na ficha igual ao painel (D21) -----------------------------------


def test_ficha_mostra_tambem_o_mes_anterior_esquecido(veiculo):
    _leitura(veiculo, date(2026, 8, 1))  # setembro e outubro ficaram faltando
    km = _km(veiculo)
    assert [p["texto"] for p in km] == [
        "KM de setembro ainda não anotado",
        "KM de outubro ainda não anotado",
    ]
    assert km[0]["grave"] is True  # mês que já passou
    assert km[0]["url"] == "/km/?mes=2026-09"
    assert km[1]["grave"] is False


def test_ficha_nao_cobra_mes_antes_da_primeira_leitura(veiculo):
    _leitura(veiculo, date(2026, 10, 1))
    assert _km(veiculo) == []


# --- link de preventiva com o item escolhido ----------------------------------------


def test_preventiva_sem_registro_vencida_pelo_km_da_compra_abre_informar_ultima(veiculo):
    veiculo.km_compra = 80_000  # óleo a cada 10.000: venceu aos 90.000 sem troca registrada
    veiculo.save()
    oleo = ItemPreventiva.objects.get(nome="Troca de óleo e filtro")
    pendencia = next(
        p for p in pendencias_do_veiculo(veiculo, hoje=HOJE) if p["texto"].startswith(oleo.nome)
    )
    assert pendencia["acao"] == "Informar última troca"
    assert f"?item={oleo.pk}&ultima=1" in pendencia["url"]
    assert f"volta={quote(f'/frota/veiculo/{veiculo.pk}/')}" in pendencia["url"]


def test_preventiva_com_troca_registrada_continua_registrar_que_foi_feita(veiculo):
    from apps.manutencao.models import Manutencao

    oleo = ItemPreventiva.objects.get(nome="Troca de óleo e filtro")
    Manutencao.objects.create(
        veiculo=veiculo, item=oleo, tipo="preventiva", data=date(2026, 1, 5), km=84_000
    )
    pendencia = next(
        p for p in pendencias_do_veiculo(veiculo, hoje=HOJE) if p["texto"].startswith(oleo.nome)
    )
    assert pendencia["acao"] == "Registrar que foi feita"
    assert f"?item={oleo.pk}&volta=" in pendencia["url"]
    assert "ultima" not in pendencia["url"]


# --- C14 também no Admin ------------------------------------------------------------


def test_c14_admin_bloqueia_fora_de_locacao_com_alocacao_ativa(alocacao, veiculo):
    form = VeiculoAdminForm({"uso": Veiculo.Uso.FORA_LOCACAO}, instance=veiculo)
    form.is_valid()
    assert "Encerre a alocação" in form.errors["uso"][0]


def test_c14_admin_carro_livre_pode_ir_para_fora_de_locacao(veiculo):
    form = VeiculoAdminForm({"uso": Veiculo.Uso.FORA_LOCACAO}, instance=veiculo)
    form.is_valid()
    assert "uso" not in form.errors


# --- KM menor no cadastro aponta para "Corrigir" ------------------------------------


def test_km_menor_no_cadastro_manda_usar_corrigir_e_nao_o_admin(veiculo):
    dados = {
        "placa": veiculo.placa,
        "marca_modelo": veiculo.marca_modelo,
        "uso": veiculo.uso,
        "km_atual": 10,
        "chave_reserva": veiculo.chave_reserva,
    }
    form = VeiculoForm(dados, instance=veiculo)
    assert not form.is_valid()
    erro = form.errors["km_atual"][0]
    assert "Corrigir" in erro and "Registros de KM" in erro
    assert "Admin" not in erro
    assert "95.000" in erro


# --- tela de venda ------------------------------------------------------------------


def test_venda_com_carro_alugado_mostra_o_motivo_sem_formulario(usuario_logado, alocacao, veiculo):
    html = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/vender/").content.decode()
    assert "Ainda não dá para registrar a venda" in html
    assert "Encerrar a alocação" in html
    assert 'name="valor"' not in html


def test_venda_ja_vem_com_o_valor_estimado(usuario_logado, veiculo):
    html = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/vender/").content.decode()
    assert 'value="45.000,00"' in html
    assert "Troque pelo valor que você recebeu" in html


def test_venda_com_erro_mantem_o_valor_digitado(usuario_logado, veiculo):
    resposta = usuario_logado.post(
        f"/frota/veiculo/{veiculo.pk}/vender/",
        {"data": "2026-10-20", "valor": "41.000,00", "km": "-5"},
    )
    html = resposta.content.decode()
    assert 'value="41.000,00"' in html
    assert 'value="45.000,00"' not in html


# --- revisão adversarial: casos que faltavam -------------------------------------


@pytest.fixture
def troca_com_substituto(alocacao):
    from apps.alocacoes.models import TrocaTemporaria

    substituto = Veiculo.objects.create(placa="SUB1A23", marca_modelo="Onix", km_atual=50_000)
    return TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=date(2026, 10, 1),
        km_retirada=50_000,
    )


def test_venda_do_substituto_em_troca_mostra_registrar_devolucao(
    usuario_logado, troca_com_substituto
):
    substituto = troca_com_substituto.veiculo_substituto
    html = usuario_logado.get(f"/frota/veiculo/{substituto.pk}/vender/").content.decode()
    assert "Registrar a devolução" in html
    assert 'name="valor"' not in html


def test_venda_de_carro_ja_vendido_nao_mostra_formulario(usuario_logado, veiculo):
    veiculo.status = Veiculo.Status.VENDIDO
    veiculo.save()
    html = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/vender/").content.decode()
    assert "já foi vendido" in html
    assert 'name="valor"' not in html


def test_c14_admin_bloqueia_substituto_em_troca_aberta(troca_com_substituto):
    substituto = troca_com_substituto.veiculo_substituto
    form = VeiculoAdminForm({"uso": Veiculo.Uso.FORA_LOCACAO}, instance=substituto)
    form.is_valid()
    assert "troca" in form.errors["uso"][0]


def test_c14_tela_do_admin_mostra_o_erro_sem_quebrar(admin_client, alocacao, veiculo):
    url = f"/admin/frota/veiculo/{veiculo.pk}/change/"
    assert admin_client.get(url).status_code == 200
    resposta = admin_client.post(url, {"uso": Veiculo.Uso.FORA_LOCACAO})
    assert resposta.status_code == 200  # volta o formulário com erro, não salva
    veiculo.refresh_from_db()
    assert veiculo.uso == Veiculo.Uso.LOCACAO
