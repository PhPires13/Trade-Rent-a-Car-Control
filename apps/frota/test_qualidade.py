"""Regressões da revisão de qualidade da frota (venda validada, detalhe e formulário)."""

from datetime import date
from decimal import Decimal

import pytest

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.frota.models import Veiculo
from apps.frota.views import SECOES_VEICULO, VeiculoForm
from apps.pessoas.models import Cliente


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_atual=95_000)


@pytest.fixture
def substituto(db):
    return Veiculo.objects.create(placa="RNB9J66", marca_modelo="Voyage", km_atual=50_000)


@pytest.fixture
def troca(veiculo, substituto):
    cliente = Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")
    alocacao = Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=95_000,
    )
    return TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=date(2026, 7, 10),
        km_retirada=50_000,
        valor_semanal_ajustado=Decimal("750.00"),
    )


def _vender(cliente_http, veiculo, **dados):
    corpo = {"data": "2026-08-01", "valor": "35000,00", "comprador": "", "custos": "", "km": ""}
    corpo.update(dados)
    return cliente_http.post(f"/frota/veiculo/{veiculo.pk}/vender/", corpo)


def test_venda_com_km_negativo_vira_mensagem_e_nao_erro_500(usuario_logado, veiculo):
    resposta = _vender(usuario_logado, veiculo, km="-5")
    assert resposta.status_code == 200
    assert "Venda não registrada" in resposta.content.decode()
    assert "KM na venda" in resposta.content.decode()
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.DISPONIVEL
    assert veiculo.km_venda is None


def test_venda_com_comprador_longo_demais_nao_grava(usuario_logado, veiculo):
    resposta = _vender(usuario_logado, veiculo, comprador="x" * 150)
    assert resposta.status_code == 200
    assert "Comprador" in resposta.content.decode()
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.DISPONIVEL


def test_venda_com_centavos_demais_nao_grava(usuario_logado, veiculo):
    resposta = _vender(usuario_logado, veiculo, valor="35.000,555")
    assert resposta.status_code == 200
    assert "Valor da venda" in resposta.content.decode()
    veiculo.refresh_from_db()
    assert veiculo.valor_venda is None


def test_detalhe_do_carro_na_oficina_mostra_o_substituto(usuario_logado, veiculo, troca):
    conteudo = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/").content.decode()
    assert "Arlen" in conteudo
    assert "(substituto)" not in conteudo
    assert "O motorista está com o substituto" in conteudo
    assert "RNB9J66" in conteudo


def test_detalhe_do_substituto_mostra_motorista_e_valor_ajustado(usuario_logado, substituto, troca):
    conteudo = usuario_logado.get(f"/frota/veiculo/{substituto.pk}/").content.decode()
    assert "Arlen (substituto)" in conteudo
    assert "750,00" in conteudo


def test_detalhe_roda_com_queries_fixas(
    usuario_logado, veiculo, troca, django_assert_max_num_queries
):
    # 12 do detalhe original + 8 fixas da faixa de pendências (4 das preventivas,
    # 2 do KM esquecido — mesma regra do painel —, multas sem FICI e sinistros
    # abertos) + folga de 1 — não crescem com o número de registros do carro.
    with django_assert_max_num_queries(21):
        assert usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/").status_code == 200


def test_formulario_do_veiculo_exibe_todos_os_campos_nas_secoes(db):
    nas_secoes = [nome for _, nomes in SECOES_VEICULO for nome in nomes]
    form = VeiculoForm()
    assert list(form.fields) == nas_secoes
    assert "status" not in form.fields
    assert form.fields["licenciamento_vencimento"].widget.input_type == "date"
