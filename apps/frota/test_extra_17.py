"""Achados extras da etapa 17 na frota: venda, ficha do vendido, IPVA e cadastro do carro."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.frota.models import PagamentoIpva, Veiculo
from apps.frota.views import VeiculoForm
from apps.pessoas.models import Cliente

HOJE = timezone.localdate()


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(
        placa="QEX1A17",
        marca_modelo="Onix",
        km_atual=50_000,
        km_compra=40_000,
        valor_compra=Decimal("50000"),
        data_aquisicao=date(2025, 1, 10),
    )


def _vender(cliente_http, veiculo, **dados):
    corpo = {"data": HOJE.isoformat(), "valor": "35000,00", "comprador": "", "custos": "", "km": ""}
    corpo.update(dados)
    return cliente_http.post(f"/frota/veiculo/{veiculo.pk}/vender/", corpo, follow=True)


# --- Venda ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dados, mensagem",
    [
        ({"valor": "-5,00"}, "Informe quanto recebeu na venda"),
        ({"valor": "0"}, "Informe quanto recebeu na venda"),
        ({"custos": "-100,00"}, "Os custos da venda não podem ser negativos"),
    ],
)
def test_venda_recusa_valor_zero_negativo_e_custos_negativos(
    usuario_logado, veiculo, dados, mensagem
):
    resposta = _vender(usuario_logado, veiculo, **dados)
    assert mensagem in resposta.content.decode()
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.DISPONIVEL
    assert veiculo.valor_venda is None


def test_ficha_do_vendido_nao_recomenda_continuar_alugando(usuario_logado, veiculo):
    _vender(usuario_logado, veiculo, valor="35.000,00")
    html = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/ficha/").content.decode()
    assert "continue alugando" not in html
    assert "saudável para continuar alugando" not in html
    assert f"Vendido em {HOJE:%d/%m/%Y} por R$" in html
    assert "resultado final" in html


# --- IPVA -----------------------------------------------------------------------------


def _post_ipva(cliente_http, veiculo, **linha):
    dados = {"pagamentos_ipva-TOTAL_FORMS": "1", "pagamentos_ipva-INITIAL_FORMS": "0"}
    dados.update({f"pagamentos_ipva-0-{k}": v for k, v in linha.items()})
    return cliente_http.post(f"/frota/veiculo/{veiculo.pk}/ipva/", dados)


def test_ipva_pago_no_futuro_recusado(usuario_logado, veiculo):
    resposta = _post_ipva(
        usuario_logado,
        veiculo,
        ano=str(HOJE.year),
        parcela="1",
        valor="1.000,00",
        pago_em=(HOJE + timedelta(days=5)).isoformat(),
    )
    assert "A data do pagamento não pode ser depois de hoje" in resposta.content.decode()
    assert not PagamentoIpva.objects.exists()


def test_ipva_pago_hoje_aceito(usuario_logado, veiculo):
    _post_ipva(
        usuario_logado,
        veiculo,
        ano=str(HOJE.year),
        parcela="1",
        valor="1.000,00",
        pago_em=HOJE.isoformat(),
    )
    assert PagamentoIpva.objects.filter(pago_em=HOJE).exists()


@pytest.mark.parametrize("ano", ["1990", "3000"])
def test_ipva_ano_fora_da_faixa_tem_uma_mensagem_so(usuario_logado, veiculo, ano):
    resposta = _post_ipva(usuario_logado, veiculo, ano=ano, parcela="1", valor="1.000,00")
    erros = resposta.context["formset"].forms[0].errors["ano"]
    assert erros == [f"Ano do IPVA entre 2000 e {HOJE.year + 1}."]
    assert not PagamentoIpva.objects.exists()


# --- Cadastro do carro ----------------------------------------------------------------


def _form(veiculo=None, **extra):
    dados = {
        "placa": veiculo.placa if veiculo else "RRX1B22",
        "marca_modelo": "Gol",
        "uso": Veiculo.Uso.LOCACAO,
        "km_atual": veiculo.km_atual if veiculo else "",
        "km_compra": veiculo.km_compra if veiculo else "",
        "chave_reserva": Veiculo.ChaveReserva.DUVIDA,
    }
    if veiculo:
        dados["data_aquisicao"] = veiculo.data_aquisicao or ""
    dados.update(extra)
    return VeiculoForm(data=dados, instance=veiculo)


def test_cadastro_recusa_compra_no_futuro(db):
    form = _form(data_aquisicao=(HOJE + timedelta(days=1)).isoformat())
    assert not form.is_valid()
    assert "não pode ser depois de hoje" in form.errors["data_aquisicao"][0]


def test_edicao_recusa_compra_depois_da_venda(veiculo):
    veiculo.status = Veiculo.Status.VENDIDO
    veiculo.data_venda = date(2025, 6, 1)
    veiculo.valor_venda = Decimal("30000")
    veiculo.save()
    form = _form(veiculo, data_aquisicao="2025-07-01")
    assert not form.is_valid()
    assert "A compra não pode ser depois da venda (01/06/2025)" in form.errors["data_aquisicao"][0]


@pytest.mark.parametrize("ano", ["abc", "2099/2100", "20/25", "1800", "2020/2019"])
def test_cadastro_recusa_ano_invalido(db, ano):
    form = _form(ano=ano)
    assert not form.is_valid()
    assert "ex.: 20/21" in form.errors["ano"][0]


@pytest.mark.parametrize(
    "ano, gravado",
    [
        ("20/21", "20/21"),
        ("2020/2021", "2020/2021"),
        ("2021", "2021"),
        ("98/99", "98/99"),
        ("20 / 20", "20/20"),
    ],
)
def test_cadastro_aceita_ano_valido(db, ano, gravado):
    form = _form(ano=ano)
    assert form.is_valid(), form.errors
    assert form.cleaned_data["ano"] == gravado


def test_ano_antigo_fora_do_padrao_continua_editavel(veiculo):
    Veiculo.objects.filter(pk=veiculo.pk).update(ano="abc")
    veiculo.refresh_from_db()
    assert _form(veiculo, ano="abc").is_valid()


@pytest.mark.parametrize("renavam", ["abc", "123", "123456789012"])
def test_cadastro_recusa_renavam_invalido(db, renavam):
    form = _form(renavam=renavam)
    assert not form.is_valid()
    assert form.errors["renavam"] == ["Renavam tem só números (11 dígitos)."]


def test_cadastro_limpa_pontos_do_renavam(db):
    form = _form(renavam="0012.345.678-9")
    assert form.is_valid(), form.errors
    assert form.cleaned_data["renavam"] == "00123456789"


def test_garantia_da_bateria_antes_da_troca_recusada(db):
    form = _form(bateria_data_troca="2025-06-01", bateria_garantia_fim="2025-01-01")
    assert not form.is_valid()
    assert "A garantia termina antes da troca" in form.errors["bateria_garantia_fim"][0]


# --- Carro principal com troca aberta / filtros ---------------------------------------


def test_carro_com_substituto_mostra_texto_e_atalho_da_manutencao(usuario_logado, veiculo):
    substituto = Veiculo.objects.create(placa="SUB1A17", marca_modelo="Gol", km_atual=1000)
    cliente = Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")
    alocacao = Alocacao.objects.create(
        cliente=cliente,
        veiculo=veiculo,
        data_inicio=HOJE - timedelta(days=10),
        valor_semanal=Decimal("500"),
        km_entrega=50_000,
    )
    TrocaTemporaria.objects.create(
        alocacao=alocacao, veiculo_substituto=substituto, data_retirada=HOJE, km_retirada=1000
    )
    html = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/").content.decode()
    assert "Enquanto este carro está parado" not in html
    assert "O motorista está com o substituto" in html
    assert f"Registrar a manutenção do {veiculo.placa}" in html


def test_filtro_de_uso_explica_fora_de_locacao(usuario_logado, veiculo):
    html = usuario_logado.get("/frota/").content.decode()
    assert "Fora de locação = carro de uso próprio" in html
