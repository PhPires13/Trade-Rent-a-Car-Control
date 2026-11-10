"""Etapa 18 — frota: mensagem do Renavam e carro vendido sem data da venda."""

from datetime import date
from decimal import Decimal

import pytest
from django.forms.models import model_to_dict
from django.urls import reverse
from django.utils import timezone

from apps.frota import desmobilizacao
from apps.frota.admin import VeiculoAdminForm
from apps.frota.models import Veiculo
from apps.frota.views import VeiculoForm

HOJE = timezone.localdate()


def _form_novo(**extra):
    dados = {
        "placa": "RRX1B22",
        "marca_modelo": "Gol",
        "uso": Veiculo.Uso.LOCACAO,
        "km_atual": "",
        "km_compra": "",
        "chave_reserva": Veiculo.ChaveReserva.DUVIDA,
    }
    dados.update(extra)
    return VeiculoForm(data=dados)


# --- Renavam ----------------------------------------------------------------------


@pytest.mark.parametrize("renavam", ["1234567", "12345678901234", "abc"])
def test_renavam_errado_diz_de_9_a_11_numeros(db, renavam):
    form = _form_novo(renavam=renavam)
    assert not form.is_valid()
    assert form.errors["renavam"] == [
        "Renavam tem de 9 a 11 números — confira no documento do carro."
    ]


@pytest.mark.parametrize("renavam", ["123456789", "1234567890", "123.456.789-01"])
def test_renavam_de_9_a_11_numeros_e_aceito(db, renavam):
    form = _form_novo(renavam=renavam)
    assert form.is_valid(), form.errors
    assert form.cleaned_data["renavam"].isdigit()


# --- Vendido sem data da venda ------------------------------------------------------


@pytest.fixture
def vendido_sem_data(db):
    # dado antigo ou gravado pelo Admin antes da regra nova
    return Veiculo.objects.create(
        placa="VSD1A00",
        marca_modelo="Onix",
        km_atual=80_000,
        data_aquisicao=date(HOJE.year, 1, 1),
        valor_compra=Decimal("40000"),
        mensalidade_protecao=Decimal("200"),
        status=Veiculo.Status.VENDIDO,
    )


def test_protecao_de_vendido_sem_data_nao_soma_ate_hoje(vendido_sem_data):
    """Mesmo critério do relatório mensal, que não conta esse carro: a ficha também não."""
    assert desmobilizacao.meses_de_protecao(vendido_sem_data, HOJE) == 0
    assert desmobilizacao.montar_ficha(vendido_sem_data).despesa_protecao_estimada == 0


def test_protecao_de_vendido_com_data_continua_ate_a_venda(vendido_sem_data):
    vendido_sem_data.data_venda = date(HOJE.year, 1, 20)
    assert desmobilizacao.meses_de_protecao(vendido_sem_data, HOJE) == 1


def test_ficha_avisa_que_falta_a_data_da_venda(usuario_logado, vendido_sem_data):
    resposta = usuario_logado.get(reverse("frota:ficha", args=[vendido_sem_data.pk]))
    assert resposta.status_code == 200
    html = resposta.content.decode()
    assert "sem data da venda: proteção não calculada" in html
    assert reverse("admin:frota_veiculo_change", args=[vendido_sem_data.pk]) in html


def _dados_admin(veiculo, **extra):
    dados = {
        chave: valor
        for chave, valor in model_to_dict(veiculo).items()
        if valor is not None and chave not in ("foto", "documento")
    }
    dados.update(extra)
    return dados


def test_admin_recusa_vendido_sem_data_da_venda(vendido_sem_data):
    form = VeiculoAdminForm(data=_dados_admin(vendido_sem_data), instance=vendido_sem_data)
    assert not form.is_valid()
    assert form.errors["data_venda"] == ["Carro vendido precisa da data da venda."]


def test_admin_aceita_vendido_com_data_da_venda(vendido_sem_data):
    form = VeiculoAdminForm(
        data=_dados_admin(vendido_sem_data, data_venda=HOJE.isoformat()),
        instance=vendido_sem_data,
    )
    assert form.is_valid(), form.errors


def test_ficha_leva_direto_ao_campo_da_data_da_venda(usuario_logado, vendido_sem_data):
    resposta = usuario_logado.get(reverse("frota:ficha", args=[vendido_sem_data.pk]))
    url = reverse("admin:frota_veiculo_change", args=[vendido_sem_data.pk])
    assert f"{url}#id_data_venda" in resposta.content.decode()


def test_admin_recusa_data_da_venda_em_carro_nao_vendido(db):
    """Com a data, o relatório contaria a venda e pararia de cobrar a proteção."""
    carro = Veiculo.objects.create(placa="DVN1A00", marca_modelo="Gol", km_atual=10_000)
    form = VeiculoAdminForm(data=_dados_admin(carro, data_venda=HOJE.isoformat()), instance=carro)
    assert not form.is_valid()
    assert "Só carro vendido tem data da venda" in form.errors["data_venda"][0]


def test_admin_aceita_carro_comum_sem_data_da_venda(db):
    carro = Veiculo.objects.create(placa="DVN1A01", marca_modelo="Gol", km_atual=10_000)
    form = VeiculoAdminForm(data=_dados_admin(carro), instance=carro)
    assert form.is_valid(), form.errors


# --- Cadastro antigo fora do padrão segue editável -------------------------------------


def _form_edicao(veiculo, **extra):
    dados = {
        chave: valor
        for chave, valor in model_to_dict(veiculo, fields=VeiculoForm._meta.fields).items()
        if valor is not None and chave not in ("foto", "documento")
    }
    dados.update(extra)
    return VeiculoForm(data=dados, instance=veiculo)


def test_renavam_e_ano_antigos_com_espacos_nao_travam_outra_edicao(db):
    """Dado antigo "123 45" / "20 / 2x" não impede mudar só o modelo do carro."""
    carro = Veiculo.objects.create(
        placa="ANT1A00",
        marca_modelo="Gol",
        km_atual=10_000,
        renavam="123 45",
        ano="20 / 2x",
    )
    form = _form_edicao(carro, marca_modelo="Gol 1.0")
    assert form.is_valid(), form.errors


def test_renavam_trocado_continua_conferido_na_edicao(db):
    carro = Veiculo.objects.create(
        placa="ANT1A01", marca_modelo="Gol", km_atual=10_000, renavam="123 45"
    )
    form = _form_edicao(carro, renavam="999")
    assert not form.is_valid()
    assert "renavam" in form.errors


# --- Placa repetida -------------------------------------------------------------------


def test_placa_repetida_mostra_link_clicavel_para_o_outro_carro(usuario_logado):
    outro = Veiculo.objects.create(placa="REP1A00", marca_modelo="Gol", km_atual=1)
    resposta = usuario_logado.post(
        reverse("frota:veiculo_novo"),
        {
            "placa": "rep-1a00",
            "marca_modelo": "Onix",
            "uso": Veiculo.Uso.LOCACAO,
            "chave_reserva": Veiculo.ChaveReserva.DUVIDA,
        },
    )
    assert resposta.status_code == 200
    html = resposta.content.decode()
    # o link chega como HTML de verdade, não como texto escapado
    assert f'<a href="{reverse("frota:detalhe", args=[outro.pk])}"' in html
    assert "&lt;a href" not in html
    assert Veiculo.objects.count() == 1
