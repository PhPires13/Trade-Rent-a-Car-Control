"""Cobrança automática de excedente de km — etapa 9 (docs.md §4.8)."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.db.models import ProtectedError

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.financeiro.models import Cobranca
from apps.financeiro.services import marcar_atrasos
from apps.frota.models import Veiculo
from apps.km.excedente import gerar_cobranca_excedente, gerar_excedentes_pendentes
from apps.km.models import RegistroKm
from apps.pessoas.models import Cliente


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_compra=50_000)


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


def alocar_limitado(veiculo, cliente, inicio, km_entrega, **kwargs):
    dados = {
        "valor_semanal": Decimal("650.00"),
        "limite_km": Alocacao.LimiteKm.LIMITADO,
        "franquia_km_mensal": 3_000,
        "taxa_km_excedido": Decimal("0.50"),
    }
    dados.update(kwargs)
    return Alocacao.objects.create(
        veiculo=veiculo, cliente=cliente, data_inicio=inicio, km_entrega=km_entrega, **dados
    )


@pytest.fixture
def alocacao_limitada(veiculo, cliente):
    return alocar_limitado(veiculo, cliente, date(2026, 6, 15), 50_000)


def registrar(veiculo, data_leitura, km):
    registro = RegistroKm(
        veiculo=veiculo,
        mes_referencia=data_leitura.replace(day=1),
        data_leitura=data_leitura,
        km=km,
    )
    registro.full_clean()
    registro.save()
    return registro


def test_estouro_gera_cobranca_com_valor_e_vencimento(alocacao_limitada, veiculo, cliente):
    registro = registrar(veiculo, date(2026, 7, 15), 54_000)  # 4.000 km em 30 dias
    cobranca = gerar_cobranca_excedente(registro, hoje=date(2026, 7, 15))
    assert cobranca is not None
    assert cobranca.cliente == cliente
    assert cobranca.origem == Cobranca.Origem.EXCEDENTE_KM
    assert cobranca.valor == Decimal("500.00")  # 1.000 km × R$ 0,50
    assert cobranca.vencimento == date(2026, 7, 15) + timedelta(days=7)
    assert "1000 km além da franquia de 3000 km" in cobranca.descricao
    registro.refresh_from_db()
    assert registro.cobranca_excedente == cobranca


def test_franquia_rateada_pelos_dias_do_periodo(alocacao_limitada, veiculo):
    registrar(veiculo, date(2026, 6, 25), 52_000)
    registro = registrar(veiculo, date(2026, 7, 10), 54_000)  # 15 dias, 2.000 km
    cobranca = gerar_cobranca_excedente(registro)
    # franquia rateada: 3.000 × 15/30 = 1.500 km → excedente 500 km × R$ 0,50
    assert cobranca.valor == Decimal("250.00")
    assert "franquia de 1500 km" in cobranca.descricao


def test_dentro_da_franquia_nao_gera(alocacao_limitada, veiculo):
    registro = registrar(veiculo, date(2026, 7, 15), 52_500)  # 2.500 km < 3.000
    assert gerar_cobranca_excedente(registro) is None


def test_nao_cobra_km_do_motorista_anterior(veiculo, cliente):
    registrar(veiculo, date(2026, 6, 10), 52_000)  # rodado pelo motorista anterior
    alocar_limitado(veiculo, cliente, date(2026, 6, 20), km_entrega=53_000)
    registro = registrar(veiculo, date(2026, 7, 10), 55_500)
    cobranca = gerar_cobranca_excedente(registro)
    # conta a partir da entrega (53.000): 2.500 km em 20 dias de alocação →
    # franquia 3.000 × 20/30 = 2.000 km → excedente 500 km × R$ 0,50
    assert cobranca.valor == Decimal("250.00")


def test_primeira_leitura_de_carro_usado_nao_cobra_a_vida_inteira(cliente, db):
    usado = Veiculo.objects.create(placa="RNB9J66", marca_modelo="Voyage", km_compra=10_000)
    alocar_limitado(usado, cliente, date(2026, 7, 1), km_entrega=80_000)
    registro = registrar(usado, date(2026, 7, 31), 84_500)  # km_anterior cai p/ km_compra
    cobranca = gerar_cobranca_excedente(registro)
    # base é a entrega (80.000), não a compra: 4.500 km − franquia 3.000 → 1.500 km
    assert cobranca.valor == Decimal("750.00")


def test_leitura_atrasada_nao_nasce_vencida_nem_derruba_o_cliente(
    alocacao_limitada, veiculo, cliente
):
    registro = registrar(veiculo, date(2026, 7, 1), 54_500)  # digitada só em 11/08
    cobranca = gerar_cobranca_excedente(registro, hoje=date(2026, 8, 11))
    # o prazo de 7 dias conta de hoje, não da leitura — senão a rotina marcaria
    # atraso e inadimplência no mesmo instante, sem o cliente nunca ter recebido
    assert cobranca.vencimento == date(2026, 8, 18)
    marcar_atrasos(hoje=date(2026, 8, 11))
    cobranca.refresh_from_db()
    cliente.refresh_from_db()
    assert cobranca.status == Cobranca.Status.PENDENTE
    assert cliente.status == Cliente.Status.ATIVO


def test_acerto_final_de_alocacao_encerrada_no_periodo(alocacao_limitada, veiculo):
    registrar(veiculo, date(2026, 6, 20), 52_000)
    alocacao_limitada.encerrar(date(2026, 7, 5), 55_000)
    registro = registrar(veiculo, date(2026, 7, 15), 55_050)  # 50 km foram no pátio
    cobranca = gerar_cobranca_excedente(registro, hoje=date(2026, 7, 15))
    # do dia 20/06 à devolução em 05/07: 3.000 km em 15 dias → franquia 1.500 km
    assert cobranca.valor == Decimal("750.00")


def test_troca_temporaria_cobra_excedente_do_substituto(veiculo, cliente, db):
    alocacao = alocar_limitado(veiculo, cliente, date(2026, 6, 1), km_entrega=50_000)
    registrar(veiculo, date(2026, 6, 30), 52_000)
    substituto = Veiculo.objects.create(placa="RNB9J66", marca_modelo="Voyage", km_compra=19_000)
    TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=date(2026, 7, 1),
        km_retirada=20_000,
    )
    leitura_principal = registrar(veiculo, date(2026, 7, 31), 52_010)  # parado na oficina
    leitura_substituto = registrar(substituto, date(2026, 7, 31), 26_000)
    assert gerar_cobranca_excedente(leitura_principal, hoje=date(2026, 7, 31)) is None
    cobranca = gerar_cobranca_excedente(leitura_substituto, hoje=date(2026, 7, 31))
    # 6.000 km no substituto contra franquia de 3.000 → 3.000 km × R$ 0,50
    assert cobranca is not None
    assert cobranca.cliente == cliente
    assert cobranca.valor == Decimal("1500.00")


def test_troca_devolvida_apura_ate_o_km_da_devolucao(veiculo, cliente, db):
    alocacao = alocar_limitado(veiculo, cliente, date(2026, 6, 1), km_entrega=50_000)
    substituto = Veiculo.objects.create(placa="RNB9J66", marca_modelo="Voyage", km_compra=19_000)
    TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=date(2026, 7, 1),
        km_retirada=20_000,
        data_devolucao=date(2026, 7, 20),
        km_devolucao=25_000,
    )
    registro = registrar(substituto, date(2026, 7, 31), 25_030)  # 30 km após devolver
    cobranca = gerar_cobranca_excedente(registro, hoje=date(2026, 7, 31))
    # 5.000 km do cliente em 19 dias → franquia 1.900 km → 3.100 km × R$ 0,50
    assert cobranca.valor == Decimal("1550.00")


def test_idempotente_nao_duplica(alocacao_limitada, veiculo):
    registro = registrar(veiculo, date(2026, 7, 15), 54_000)
    assert gerar_cobranca_excedente(registro) is not None
    assert gerar_cobranca_excedente(registro) is None
    assert Cobranca.objects.filter(origem=Cobranca.Origem.EXCEDENTE_KM).count() == 1


def test_apagar_cobranca_vinculada_e_bloqueado(alocacao_limitada, veiculo):
    registro = registrar(veiculo, date(2026, 7, 15), 54_000)
    cobranca = gerar_cobranca_excedente(registro)
    with pytest.raises(ProtectedError):  # o caminho certo é cancelar no Financeiro
        cobranca.delete()


def test_sem_taxa_configurada_nao_gera(alocacao_limitada, veiculo):
    alocacao_limitada.taxa_km_excedido = None
    alocacao_limitada.save()
    registro = registrar(veiculo, date(2026, 7, 15), 54_000)
    assert gerar_cobranca_excedente(registro) is None


def test_alocacao_livre_nao_gera(veiculo, cliente):
    Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 6, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=50_000,
    )
    registro = registrar(veiculo, date(2026, 7, 15), 60_000)
    assert gerar_cobranca_excedente(registro) is None


def test_alocacao_encerrada_antes_da_leitura_nao_gera(alocacao_limitada, veiculo):
    alocacao_limitada.data_termino = date(2026, 7, 1)
    alocacao_limitada.save()
    registro = registrar(veiculo, date(2026, 7, 15), 54_000)
    assert gerar_cobranca_excedente(registro) is None


def test_rotina_gera_excedentes_pendentes(alocacao_limitada, veiculo):
    registrar(veiculo, date(2026, 7, 15), 54_000)
    criadas = gerar_excedentes_pendentes(hoje=date(2026, 7, 20))
    assert len(criadas) == 1
    assert gerar_excedentes_pendentes(hoje=date(2026, 7, 20)) == []  # catch-up idempotente


def test_rotina_ignora_leituras_antigas(veiculo, cliente):
    alocar_limitado(veiculo, cliente, date(2026, 4, 1), km_entrega=50_000)
    registrar(veiculo, date(2026, 5, 15), 60_000)  # estouro claro, mas antigo
    # leitura de 15/05 fica fora da janela de 45 dias — nada de cobrança
    # retroativa já vencida na primeira rotina após o deploy
    assert gerar_excedentes_pendentes(hoje=date(2026, 7, 20)) == []
    assert len(gerar_excedentes_pendentes(hoje=date(2026, 5, 20))) == 1


def test_registro_pela_tela_gera_cobranca_e_avisa(usuario_logado, alocacao_limitada, veiculo):
    resposta = usuario_logado.post(
        f"/km/registrar/{veiculo.pk}/",
        {"data_leitura": "2026-07-15", "km": "54000"},
        follow=True,
    )
    mensagens = [str(m) for m in resposta.context["messages"]]
    assert any("cancele no Financeiro" in m for m in mensagens)
    assert Cobranca.objects.filter(origem=Cobranca.Origem.EXCEDENTE_KM).count() == 1


def test_badge_de_excedente_na_lista_mensal(usuario_logado, alocacao_limitada, veiculo):
    registro = registrar(veiculo, date(2026, 7, 15), 54_000)
    gerar_cobranca_excedente(registro)
    resposta = usuario_logado.get("/km/?mes=2026-07")
    assert "excedente R$" in resposta.content.decode()
