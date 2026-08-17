from datetime import date
from decimal import Decimal

import pytest

from apps.alocacoes.models import Alocacao
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_atual=50_000)


@pytest.fixture
def outro_veiculo(db):
    return Veiculo.objects.create(placa="RNB9J66", marca_modelo="Voyage", km_atual=30_000)


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


@pytest.fixture
def alocacao(veiculo, cliente):
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        dia_vencimento=2,
        km_entrega=50_000,
    )


def dados_edicao(**extras):
    base = {
        "valor_semanal": "700.00",
        "dia_vencimento": "4",
        "caucao_valor": "",
        "limite_km": "ilimitado",
        "franquia_km_mensal": "",
        "taxa_km_excedido": "",
        "observacoes": "",
    }
    base.update(extras)
    return base


def test_tela_de_edicao_abre(usuario_logado, alocacao):
    resposta = usuario_logado.get(f"/alocacoes/{alocacao.pk}/editar/")
    assert resposta.status_code == 200
    assert "veiculo" not in resposta.context["form"].fields


def test_editar_muda_valor_e_dia_de_vencimento(usuario_logado, alocacao):
    resposta = usuario_logado.post(f"/alocacoes/{alocacao.pk}/editar/", dados_edicao())
    assert resposta.status_code == 302
    alocacao.refresh_from_db()
    assert alocacao.valor_semanal == Decimal("700.00")
    assert alocacao.dia_vencimento == 4


def test_editar_nao_permite_trocar_o_veiculo(usuario_logado, alocacao, outro_veiculo):
    veiculo_original = alocacao.veiculo_id
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/editar/",
        dados_edicao(veiculo=outro_veiculo.pk, cliente="", km_entrega="1"),
    )
    assert resposta.status_code == 302
    alocacao.refresh_from_db()
    assert alocacao.veiculo_id == veiculo_original
    assert alocacao.km_entrega == 50_000


def test_editar_limite_exige_franquia(usuario_logado, alocacao):
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/editar/", dados_edicao(limite_km="limitado")
    )
    assert resposta.status_code == 200
    assert "franquia_km_mensal" in resposta.context["form"].errors
    alocacao.refresh_from_db()
    assert alocacao.limite_km == Alocacao.LimiteKm.ILIMITADO


def test_editar_salva_franquia_e_taxa(usuario_logado, alocacao):
    usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/editar/",
        dados_edicao(limite_km="limitado", franquia_km_mensal="3000", taxa_km_excedido="0.80"),
    )
    alocacao.refresh_from_db()
    assert alocacao.franquia_km_mensal == 3_000
    assert alocacao.taxa_km_excedido == Decimal("0.80")


def test_nova_com_veiculo_na_url_pre_seleciona(usuario_logado, veiculo, db):
    disponivel = Veiculo.objects.create(placa="RUJ3I28", marca_modelo="HB20")
    resposta = usuario_logado.get(f"/alocacoes/nova/?veiculo={disponivel.pk}")
    assert resposta.status_code == 200
    assert resposta.context["form"].initial["veiculo"] == str(disponivel.pk)
    assert f'value="{disponivel.pk}" selected' in resposta.content.decode()


def test_nova_sem_parametro_continua_funcionando(usuario_logado, db):
    resposta = usuario_logado.get("/alocacoes/nova/")
    assert resposta.status_code == 200
    assert "veiculo" not in resposta.context["form"].initial


def test_lista_mostra_link_de_edicao(usuario_logado, alocacao):
    resposta = usuario_logado.get("/alocacoes/")
    assert f"/alocacoes/{alocacao.pk}/editar/" in resposta.content.decode()


def test_alocacao_encerrada_nao_pode_ser_editada(usuario_logado, alocacao):
    """Revisão etapa 8: contrato encerrado é registro histórico."""
    alocacao.encerrar(data_termino=alocacao.data_inicio, km_devolucao=alocacao.km_entrega)
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/editar/",
        {
            "valor_semanal": "999.00",
            "dia_vencimento": alocacao.dia_vencimento,
            "limite_km": alocacao.limite_km,
            "observacoes": "",
        },
    )
    assert resposta.status_code == 302  # redirect com mensagem de erro, sem salvar
    alocacao.refresh_from_db()
    assert str(alocacao.valor_semanal) != "999.00"
