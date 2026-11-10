"""Etapa 18c — frota: sobras da etapa 18 e refatoração com o comportamento travado.

Ficha do carro separa o aluguel do excedente/encargo, vendidos em cartões no
celular, atalho "Dar carro substituto", números formatados na retaguarda e
alvo de toque do <summary> do ranking.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.financeiro import services as financeiro
from apps.financeiro.models import Cobranca
from apps.frota import desmobilizacao
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente

HOJE = timezone.localdate()


@pytest.fixture
def carro(db):
    return Veiculo.objects.create(
        placa="ETA8C18",
        marca_modelo="Onix",
        km_compra=40_000,
        km_atual=40_000,
        data_aquisicao=HOJE - timedelta(days=120),
        valor_compra=Decimal("50000.00"),
    )


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


@pytest.fixture
def alocacao(carro, cliente):
    return Alocacao.objects.create(
        veiculo=carro,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=30),
        valor_semanal=Decimal("650.00"),
        km_entrega=40_000,
    )


def _cobranca(alocacao, origem, valor):
    return Cobranca.objects.create(
        cliente=alocacao.cliente,
        alocacao=alocacao,
        origem=origem,
        descricao=f"Teste {origem}",
        valor=Decimal(valor),
        vencimento=HOJE - timedelta(days=3),
    )


def _pagar(cobranca):
    financeiro.registrar_recebimento(
        cobranca.cliente, HOJE, cobranca.valor, "pix", [(cobranca, cobranca.valor)]
    )


# --- (1) Ficha: "Aluguéis recebidos" só com o aluguel semanal ---------------------------


def test_ficha_separa_aluguel_de_excedente_e_encargo_sem_mudar_o_total(alocacao, carro):
    _pagar(_cobranca(alocacao, Cobranca.Origem.ALUGUEL, "650.00"))
    _pagar(_cobranca(alocacao, Cobranca.Origem.EXCEDENTE_KM, "120.00"))
    _pagar(_cobranca(alocacao, Cobranca.Origem.ENCARGO, "65.00"))
    ficha = desmobilizacao.montar_ficha(carro)
    assert ficha.receita_aluguel == Decimal("650.00")
    assert ficha.receita_extras_locacao == Decimal("185.00")
    # o total da receita continua o mesmo de antes (tudo do contrato entra)
    assert ficha.receita_total == Decimal("835.00")


def test_ficha_separa_desconto_de_caucao_pela_origem(alocacao, carro):
    caucao = financeiro.abrir_caucao(
        alocacao, valor_recebido=Decimal("1000.00"), data=HOJE - timedelta(days=30)
    )
    aluguel = _cobranca(alocacao, Cobranca.Origem.ALUGUEL, "650.00")
    excedente = _cobranca(alocacao, Cobranca.Origem.EXCEDENTE_KM, "100.00")
    financeiro.descontar_da_caucao(caucao, aluguel, Decimal("650.00"), HOJE)
    financeiro.descontar_da_caucao(caucao, excedente, Decimal("100.00"), HOJE)
    ficha = desmobilizacao.montar_ficha(carro)
    assert ficha.receita_aluguel == Decimal("650.00")
    assert ficha.receita_extras_locacao == Decimal("100.00")
    assert ficha.receita_total == Decimal("750.00")


def test_tela_da_ficha_mostra_a_linha_do_excedente_so_quando_ha(usuario_logado, alocacao, carro):
    _pagar(_cobranca(alocacao, Cobranca.Origem.ALUGUEL, "650.00"))
    url = reverse("frota:ficha", args=[carro.pk])
    resposta = usuario_logado.get(url)
    assert "Excedente de km, encargos" not in resposta.content.decode()
    _pagar(_cobranca(alocacao, Cobranca.Origem.ENCARGO, "65.00"))
    html = usuario_logado.get(url).content.decode()
    assert "Excedente de km, encargos e outras cobranças do contrato" in html
    assert "+ R$ 65,00" in html
    assert "+ R$ 650,00" in html


# --- (2) /frota/desmobilizacao/: vendidos em cartões no celular -------------------------


def test_vendidos_viram_cartoes_no_celular_com_o_resultado_a_vista(usuario_logado):
    Veiculo.objects.create(
        placa="VND8C18",
        marca_modelo="HB20",
        data_aquisicao=HOJE - timedelta(days=400),
        valor_compra=Decimal("40000.00"),
        status=Veiculo.Status.VENDIDO,
        data_venda=HOJE - timedelta(days=5),
        valor_venda=Decimal("45000.00"),
    )
    html = usuario_logado.get(reverse("frota:ranking")).content.decode()
    # o cartão (só no celular) mostra o resultado; a tabela só aparece da tela média em diante
    assert '<ul class="space-y-3 md:hidden" aria-label="Carros já vendidos">' in html
    assert '<div class="hidden overflow-x-auto rounded-lg bg-white shadow md:block">' in html
    cartao = html.split('aria-label="Carros já vendidos">')[1].split("</ul>")[0]
    assert "VND8C18" in cartao
    assert "Resultado final" in cartao
    assert "R$ 5.000,00" in cartao  # 45.000 − 40.000
    assert "investido R$ 40.000,00" in cartao


# --- (3) Atalho "Dar carro substituto" no detalhe do carro ------------------------------


def _na_oficina(carro):
    from apps.manutencao.models import Manutencao

    return Manutencao.objects.create(
        veiculo=carro,
        tipo=Manutencao.Tipo.ESPORADICA,
        data=HOJE - timedelta(days=2),
        data_entrada=HOJE - timedelta(days=2),
        descricao="Motor",
    )


def test_detalhe_mostra_dar_carro_substituto_com_carro_alocado_na_oficina(
    usuario_logado, alocacao, carro
):
    url = reverse("frota:detalhe", args=[carro.pk])
    assert "Dar carro substituto" not in usuario_logado.get(url).content.decode()
    _na_oficina(carro)
    html = usuario_logado.get(url).content.decode()
    assert "Dar carro substituto" in html
    link = reverse("alocacoes:troca_nova", args=[alocacao.pk])
    assert (
        f"{link}?motivo=Manuten%C3%A7%C3%A3o%20do%20carro%20principal&amp;voltar="
        f"/frota/veiculo/{carro.pk}/" in html
    )


def test_detalhe_sem_atalho_quando_nao_ha_alocacao_ou_ja_tem_substituto(
    usuario_logado, alocacao, carro
):
    from apps.alocacoes.models import TrocaTemporaria

    _na_oficina(carro)
    reserva = Veiculo.objects.create(placa="RES8C18", marca_modelo="Gol", km_atual=1000)
    TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=reserva,
        data_retirada=HOJE - timedelta(days=1),
        km_retirada=1000,
    )
    url = reverse("frota:detalhe", args=[carro.pk])
    assert "Dar carro substituto" not in usuario_logado.get(url).content.decode()
    # carro sem contrato, só na oficina: não há a quem dar substituto
    sozinho = Veiculo.objects.create(placa="SOL8C18", marca_modelo="Gol")
    _na_oficina(sozinho)
    html = usuario_logado.get(reverse("frota:detalhe", args=[sozinho.pk])).content.decode()
    assert "Dar carro substituto" not in html


# --- (4) Retaguarda: KM e R$ formatados nas listas --------------------------------------


def test_retaguarda_formata_km_e_reais_nas_listas(admin_client, carro):
    from apps.frota.models import Categoria, PagamentoIpva

    carro.km_atual = 48_900
    carro.mensalidade_protecao = Decimal("1234.50")
    carro.save()
    PagamentoIpva.objects.create(veiculo=carro, ano=HOJE.year, valor=Decimal("1890.75"))
    Categoria.objects.create(nome="Hatch 8C", valor_semanal_referencia=Decimal("1650.00"))

    html = admin_client.get("/admin/frota/veiculo/").content.decode()
    assert "48.900" in html and "R$ 1.234,50" in html
    assert "R$ 1.890,75" in admin_client.get("/admin/frota/pagamentoipva/").content.decode()
    assert "R$ 1.650,00" in admin_client.get("/admin/frota/categoria/").content.decode()
    # a coluna continua ordenável pelo campo do banco
    assert admin_client.get("/admin/frota/veiculo/?o=7").status_code == 200


# --- (5) Ranking: <summary> com alvo de toque de 44px -----------------------------------


def test_summary_do_ranking_tem_alvo_de_toque_de_44px(usuario_logado):
    html = usuario_logado.get(reverse("frota:ranking")).content.decode()
    assert '<summary class="-my-3 cursor-pointer py-3 font-semibold">' in html
