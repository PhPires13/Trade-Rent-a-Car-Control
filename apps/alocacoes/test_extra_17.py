"""Etapa 17 (achados extras): troca repetida, trocas sobrepostas, x-data, checkbox e documentos."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente

HOJE = timezone.localdate()


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_atual=50_000)


@pytest.fixture
def sub1(db):
    return Veiculo.objects.create(placa="RUJ3I28", marca_modelo="Onix", km_atual=40_000)


@pytest.fixture
def sub2(db):
    return Veiculo.objects.create(placa="RUJ3I29", marca_modelo="HB20", km_atual=30_000)


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


@pytest.fixture
def alocacao(veiculo, cliente):
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=20),
        valor_semanal=Decimal("650.00"),
        dia_vencimento=HOJE.weekday(),
        km_entrega=50_000,
    )


def _post_troca(cliente_http, alocacao, substituto, data=HOJE, km="40000"):
    return cliente_http.post(
        f"/alocacoes/{alocacao.pk}/troca/",
        {
            "veiculo_substituto": substituto.pk,
            "data_retirada": data.isoformat(),
            "km_retirada": km,
        },
    )


# --- troca repetida (clique duplo / duas abas) não dá erro 500 ------------------


def test_troca_repetida_volta_para_a_ficha_sem_erro(usuario_logado, alocacao, sub1, sub2):
    assert _post_troca(usuario_logado, alocacao, sub1).status_code == 302
    resposta = _post_troca(usuario_logado, alocacao, sub2, km="30000")
    assert resposta.status_code == 302
    assert resposta.url == reverse("pessoas:detalhe", args=[alocacao.cliente_id])
    assert TrocaTemporaria.objects.count() == 1
    seguinte = usuario_logado.get(resposta.url)
    assert "Devolva-o antes de trocar de novo" in seguinte.content.decode()


def test_troca_em_alocacao_encerrada_volta_para_a_ficha(usuario_logado, alocacao, sub1):
    alocacao.encerrar(HOJE, km_devolucao=50_500)
    resposta = _post_troca(usuario_logado, alocacao, sub1)
    assert resposta.status_code == 302
    assert not TrocaTemporaria.objects.exists()


def test_model_clean_usa_erro_geral_para_troca_em_andamento(alocacao, sub1, sub2):
    TrocaTemporaria.objects.create(
        alocacao=alocacao, veiculo_substituto=sub1, data_retirada=HOJE, km_retirada=40_000
    )
    nova = TrocaTemporaria(
        alocacao=Alocacao.objects.get(pk=alocacao.pk),
        veiculo_substituto=sub2,
        data_retirada=HOJE,
        km_retirada=30_000,
    )
    with pytest.raises(ValidationError) as erro:
        nova.full_clean()
    assert "__all__" in erro.value.message_dict
    assert "alocacao" not in erro.value.message_dict


# --- nova troca não começa dentro do período de uma troca já devolvida ----------


def _troca_devolvida(alocacao, substituto):
    troca = TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=HOJE - timedelta(days=10),
        km_retirada=40_000,
    )
    troca.devolver(HOJE - timedelta(days=3), 40_300)
    return troca


def test_troca_nao_sobrepoe_troca_anterior_devolvida(usuario_logado, alocacao, sub1, sub2):
    _troca_devolvida(alocacao, sub1)
    resposta = _post_troca(usuario_logado, alocacao, sub2, data=HOJE - timedelta(days=8))
    assert resposta.status_code == 200
    assert "RUJ3I28" in resposta.content.decode()
    assert TrocaTemporaria.objects.count() == 1
    # no mesmo dia da devolução anterior, pode
    resposta = _post_troca(usuario_logado, alocacao, sub2, data=HOJE - timedelta(days=3))
    assert resposta.status_code == 302
    assert TrocaTemporaria.objects.count() == 2


def test_formulario_da_troca_limita_a_data_pela_ultima_devolucao(
    usuario_logado, alocacao, sub1, sub2
):
    _troca_devolvida(alocacao, sub1)
    resposta = usuario_logado.get(f"/alocacoes/{alocacao.pk}/troca/")
    assert f'min="{(HOJE - timedelta(days=3)).isoformat()}"' in resposta.content.decode()


# --- x-data do Alpine com valor digitado escapado --------------------------------


def test_limite_km_digitado_nao_vira_codigo_no_alpine(usuario_logado, veiculo, cliente):
    malicioso = "x'+alert(1)+'"
    resposta = usuario_logado.post("/alocacoes/nova/", {"limite_km": malicioso})
    html = resposta.content.decode()
    assert "limite: 'x&#x27;+alert(1)" not in html
    assert "x\\u0027+alert(1)" in html


# --- caixas de confirmação com tamanho de toque ----------------------------------


def test_checkboxes_de_alocacoes_sem_largura_automatica():
    from pathlib import Path

    from django.conf import settings

    pasta = Path(settings.BASE_DIR) / "templates" / "alocacoes"
    for arquivo in pasta.glob("*.html"):
        assert 'style="width:auto"' not in arquivo.read_text(), arquivo.name


# --- documentos sem a barra inferior fixa ------------------------------------------


def test_contrato_e_checklist_sem_barra_inferior(usuario_logado, alocacao):
    for url in (
        f"/alocacoes/{alocacao.pk}/contrato/",
        f"/alocacoes/{alocacao.pk}/vistoria/imprimir/",
    ):
        html = usuario_logado.get(url).content.decode()
        assert 'aria-label="Atalhos"' not in html, url
    ficha = usuario_logado.get(reverse("pessoas:detalhe", args=[alocacao.cliente_id]))
    assert 'aria-label="Atalhos"' in ficha.content.decode()
