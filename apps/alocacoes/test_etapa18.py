"""Etapa 18: troca gravada em dobro, contrato pré-carregado e caixas de confirmação."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from django.conf import settings
from django.db import IntegrityError
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
def substituto(db):
    return Veiculo.objects.create(placa="RUJ3I28", marca_modelo="Onix", km_atual=40_000)


@pytest.fixture
def alocacao(veiculo):
    cliente = Cliente.objects.create(nome="Arlen", cpf_cnpj="529.982.247-25")
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=20),
        valor_semanal=Decimal("650.00"),
        dia_vencimento=HOJE.weekday(),
        km_entrega=50_000,
    )


# --- troca: dois envios ao mesmo tempo não dão erro 500 ------------------------------


def test_troca_gravada_ao_mesmo_tempo_por_outro_envio_volta_para_a_ficha(
    usuario_logado, alocacao, substituto, monkeypatch
):
    def outro_envio_ja_gravou(self, *args, **kwargs):
        raise IntegrityError("uma_troca_aberta_por_alocacao")

    monkeypatch.setattr(TrocaTemporaria, "save", outro_envio_ja_gravou)
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/troca/",
        {
            "veiculo_substituto": substituto.pk,
            "data_retirada": HOJE.isoformat(),
            "km_retirada": "40.000",
        },
        follow=True,
    )
    assert resposta.status_code == 200
    assert resposta.redirect_chain[-1][0] == reverse("pessoas:detalhe", args=[alocacao.cliente_id])
    assert "Esta troca já tinha sido registrada" in resposta.content.decode()


# --- contrato: pré-carregamento do navegador não marca "impresso" ----------------------


def test_contrato_pre_carregado_nao_marca_o_passo_de_impressao(usuario_logado, alocacao):
    url = f"/alocacoes/{alocacao.pk}/contrato/"
    assert usuario_logado.get(url, headers={"Sec-Purpose": "prefetch"}).status_code == 200
    alocacao.refresh_from_db()
    assert alocacao.contrato_aberto_em is None

    usuario_logado.get(url)
    alocacao.refresh_from_db()
    assert alocacao.contrato_aberto_em == HOJE


# --- caixas de confirmação: a regra "form input {width:100%}" não estica a caixa -------


@pytest.mark.parametrize(
    "arquivo",
    [
        "alocacoes/encerrar.html",
        "alocacoes/nova.html",
        "alocacoes/troca_nova.html",
        "alocacoes/troca_devolver.html",
        "alocacoes/vistoria_form.html",
        "pessoas/cliente_form.html",
    ],
)
def test_telas_com_caixa_de_confirmacao_fixam_o_tamanho_da_caixa(arquivo):
    texto = (Path(settings.BASE_DIR) / "templates" / arquivo).read_text()
    assert 'form input[type="checkbox"] { width: 1.25rem; height: 1.25rem;' in texto


def test_devolver_substituto_mostra_caixa_com_tamanho_fixo(usuario_logado, alocacao, substituto):
    troca = TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=HOJE - timedelta(days=2),
        km_retirada=40_000,
    )
    html = usuario_logado.get(f"/alocacoes/troca/{troca.pk}/devolver/").content.decode()
    assert 'form input[type="checkbox"] { width: 1.25rem' in html


# --- duas abas: o 2º envio não refaz o encerramento nem a devolução (revisão) ----------


def test_encerrar_com_copia_velha_da_alocacao_nao_refaz_o_acerto(alocacao):
    from django.core.exceptions import ValidationError

    copia_da_outra_aba = Alocacao.objects.get(pk=alocacao.pk)
    alocacao.encerrar(data_termino=HOJE, km_devolucao=51_000)
    assert copia_da_outra_aba.status == Alocacao.Status.ATIVA  # ainda não sabe
    with pytest.raises(ValidationError, match="já encerrada"):
        copia_da_outra_aba.encerrar(data_termino=HOJE, km_devolucao=52_000)
    alocacao.refresh_from_db()
    assert alocacao.km_devolucao == 51_000


def test_devolver_substituto_com_copia_velha_da_troca_nao_devolve_de_novo(alocacao, substituto):
    from django.core.exceptions import ValidationError

    troca = TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=HOJE - timedelta(days=2),
        km_retirada=40_000,
    )
    copia_da_outra_aba = TrocaTemporaria.objects.get(pk=troca.pk)
    troca.devolver(data_devolucao=HOJE, km_devolucao=40_500)
    with pytest.raises(ValidationError, match="já devolvido"):
        copia_da_outra_aba.devolver(data_devolucao=HOJE, km_devolucao=41_000)
    troca.refresh_from_db()
    assert troca.km_devolucao == 40_500


# --- encerrar: "Receber pagamento" volta para a devolução do carro (revisão) -----------


def test_receber_da_tela_de_encerrar_traz_o_voltar(usuario_logado, alocacao):
    from apps.financeiro.models import Cobranca

    Cobranca.objects.create(
        cliente=alocacao.cliente,
        alocacao=alocacao,
        origem=Cobranca.Origem.ALUGUEL,
        valor=Decimal("650.00"),
        vencimento=HOJE - timedelta(days=3),
        status=Cobranca.Status.ATRASADO,
    )
    url = f"/alocacoes/{alocacao.pk}/encerrar/"
    html = usuario_logado.get(url).content.decode()
    assert f"?cliente={alocacao.cliente_id}&voltar={url}" in html
