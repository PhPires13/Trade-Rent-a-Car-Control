"""Bugs da frente de KM: C15, C21, D19, D20, D21, D22, D30."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.financeiro.models import Cobranca
from apps.frota.models import Veiculo
from apps.km.excedente import gerar_cobrancas_excedente, gerar_excedentes_pendentes
from apps.km.forms import CorrecaoLeituraForm
from apps.km.models import (
    AcertoExcedenteKm,
    RegistroKm,
    mes_padrao_da_lista,
    pendencias_de_leitura,
)
from apps.km.views import _mes_do_valor
from apps.pessoas.models import Cliente


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_compra=12_000)


def cliente(nome, cpf):
    return Cliente.objects.create(nome=nome, cpf_cnpj=cpf)


def alocar(veiculo, quem, inicio, km_entrega, **kwargs):
    dados = {
        "valor_semanal": Decimal("650.00"),
        "limite_km": Alocacao.LimiteKm.LIMITADO,
        "franquia_km_mensal": 3_000,
        "taxa_km_excedido": Decimal("0.50"),
    }
    dados.update(kwargs)
    return Alocacao.objects.create(
        veiculo=veiculo, cliente=quem, data_inicio=inicio, km_entrega=km_entrega, **dados
    )


def registrar(veiculo, data_leitura, km, mes=None):
    registro = RegistroKm(
        veiculo=veiculo,
        mes_referencia=mes or data_leitura.replace(day=1),
        data_leitura=data_leitura,
        km=km,
    )
    registro.full_clean()
    registro.save()
    return registro


# --- C15: dois clientes limitados no mesmo carro, no mesmo período -----------


def test_c15_cliente_que_devolveu_antes_da_leitura_tambem_paga_o_excedente(veiculo):
    ana = cliente("Ana", "111.222.333-44")
    bruno = cliente("Bruno", "222.333.444-55")
    registrar(veiculo, date(2026, 6, 30), 12_000)
    alocacao_a = alocar(veiculo, ana, date(2026, 7, 1), 12_000)
    alocacao_a.encerrar(date(2026, 7, 10), 15_000)  # 3.000 km em 9 dias
    alocacao_b = alocar(veiculo, bruno, date(2026, 7, 12), 15_000)
    julho = registrar(veiculo, date(2026, 7, 31), 18_000)

    criadas = gerar_cobrancas_excedente(julho, hoje=date(2026, 7, 31))

    por_cliente = {cobranca.cliente.nome: cobranca for cobranca in criadas}
    # Ana: 3.000 km − franquia de 9 dias (900) = 2.100 km × R$ 0,50
    assert por_cliente["Ana"].valor == Decimal("1050.00")
    assert por_cliente["Ana"].alocacao == alocacao_a
    # Bruno: 3.000 km − franquia de 19 dias (1.900) = 1.100 km × R$ 0,50
    assert por_cliente["Bruno"].valor == Decimal("550.00")
    julho.refresh_from_db()
    assert julho.cobranca_excedente.alocacao == alocacao_b
    assert AcertoExcedenteKm.objects.get(registro=julho).alocacao == alocacao_a
    # rotina diária no dia seguinte não duplica nada
    assert gerar_excedentes_pendentes(hoje=date(2026, 8, 1)) == []
    assert Cobranca.objects.filter(origem=Cobranca.Origem.EXCEDENTE_KM).count() == 2


def test_c15_rotina_cria_o_acerto_que_faltava_mesmo_com_a_cobranca_principal(veiculo):
    ana = cliente("Ana", "111.222.333-44")
    bruno = cliente("Bruno", "222.333.444-55")
    registrar(veiculo, date(2026, 6, 30), 12_000)
    alocar(veiculo, ana, date(2026, 7, 1), 12_000).encerrar(date(2026, 7, 10), 15_000)
    alocar(veiculo, bruno, date(2026, 7, 12), 15_000)
    registrar(veiculo, date(2026, 7, 31), 18_000)

    criadas = gerar_excedentes_pendentes(hoje=date(2026, 8, 1))

    assert sorted(c.cliente.nome for c in criadas) == ["Ana", "Bruno"]


def test_c15_corrigir_leitura_refaz_o_acerto_do_cliente_anterior(usuario_logado, veiculo):
    ana = cliente("Ana", "111.222.333-44")
    bruno = cliente("Bruno", "222.333.444-55")
    registrar(veiculo, date(2026, 6, 30), 12_000)
    alocar(veiculo, ana, date(2026, 7, 1), 12_000).encerrar(date(2026, 7, 10), 15_000)
    alocar(veiculo, bruno, date(2026, 7, 12), 15_000)
    julho = registrar(veiculo, date(2026, 7, 31), 18_000)
    gerar_cobrancas_excedente(julho, hoje=date(2026, 7, 31))
    acerto_antigo = AcertoExcedenteKm.objects.get(registro=julho).cobranca

    resposta = usuario_logado.post(
        f"/km/corrigir/{julho.pk}/", {"data_leitura": "2026-07-31", "km": "17500"}
    )

    assert resposta.status_code == 302
    acerto_antigo.refresh_from_db()
    assert acerto_antigo.status == Cobranca.Status.CANCELADA
    # o excedente da Ana (até a devolução, 15.000) não mudou: é refeito igual
    assert AcertoExcedenteKm.objects.get(registro=julho).cobranca.valor == Decimal("1050.00")
    julho.refresh_from_db()
    assert julho.cobranca_excedente.valor == Decimal("300.00")  # Bruno: 2.500 − 1.900


def test_c15_apagar_leitura_cancela_tambem_o_acerto(usuario_logado, veiculo):
    ana = cliente("Ana", "111.222.333-44")
    bruno = cliente("Bruno", "222.333.444-55")
    registrar(veiculo, date(2026, 6, 30), 12_000)
    alocar(veiculo, ana, date(2026, 7, 1), 12_000).encerrar(date(2026, 7, 10), 15_000)
    alocar(veiculo, bruno, date(2026, 7, 12), 15_000)
    julho = registrar(veiculo, date(2026, 7, 31), 18_000)
    gerar_cobrancas_excedente(julho, hoje=date(2026, 7, 31))
    acerto = AcertoExcedenteKm.objects.get(registro=julho).cobranca

    usuario_logado.post(f"/km/corrigir/{julho.pk}/", {"apagar": "1"})

    acerto.refresh_from_db()
    assert acerto.status == Cobranca.Status.CANCELADA
    assert not RegistroKm.objects.filter(pk=julho.pk).exists()


def test_c15_lista_mostra_o_excedente_do_cliente_anterior(usuario_logado, veiculo):
    ana = cliente("Ana", "111.222.333-44")
    registrar(veiculo, date(2026, 6, 30), 12_000)
    alocar(veiculo, ana, date(2026, 7, 1), 12_000).encerrar(date(2026, 7, 10), 15_000)
    alocar(veiculo, cliente("Bruno", "222.333.444-55"), date(2026, 7, 12), 15_000)
    julho = registrar(veiculo, date(2026, 7, 31), 18_000)
    gerar_cobrancas_excedente(julho, hoje=date(2026, 7, 31))

    conteudo = usuario_logado.get("/km/?mes=2026-07").content.decode()

    assert "excedente de Ana" in conteudo


# --- C21: mês absurdo no endereço ---------------------------------------------


def test_c21_mes_absurdo_no_endereco_volta_para_o_mes_atual(usuario_logado, veiculo):
    resposta = usuario_logado.get("/km/?mes=9999-12")
    assert resposta.status_code == 200
    assert resposta.context["mes"].year == timezone.localdate().year
    assert _mes_do_valor("9999-12") is None
    assert _mes_do_valor("0001-01") is None
    assert _mes_do_valor("2026-07") == date(2026, 7, 1)


# --- D19: mês esquecido lançado depois ----------------------------------------


def test_d19_mes_fechado_nao_sugere_data_e_pede_o_km_anotado(usuario_logado, veiculo):
    passado = (timezone.localdate().replace(day=1) - timedelta(days=40)).replace(day=1)
    conteudo = usuario_logado.get(f"/km/?mes={passado:%Y-%m}").content.decode()
    ultimo_dia = (passado + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    assert f'value="{ultimo_dia:%Y-%m-%d}"' not in conteudo
    assert "KM anotado naquela data" in conteudo
    assert "não o número do painel de hoje" in conteudo


# --- D20: primeira leitura de carro comprado há tempos -------------------------


def test_d20_primeira_leitura_de_carro_antigo_e_ponto_de_partida(db):
    antigo = Veiculo.objects.create(
        placa="RNB9J66",
        marca_modelo="Voyage",
        km_compra=20_000,
        data_aquisicao=date(2024, 3, 1),
    )
    registro = registrar(antigo, date(2026, 7, 31), 120_000)
    assert registro.km_anterior is None
    assert registro.km_utilizado is None  # e não 100.000 km "rodados agora"


def test_d20_sem_data_de_compra_tambem_e_ponto_de_partida(db):
    sem_data = Veiculo.objects.create(placa="RNB9J66", marca_modelo="Voyage", km_compra=45_000)
    registro = registrar(sem_data, date(2026, 7, 31), 4_500)  # dígito a menos
    assert registro.km_utilizado is None  # e não −40.500


def test_d20_compra_recente_continua_usando_o_km_da_compra(db):
    novo = Veiculo.objects.create(
        placa="RNB9J66",
        marca_modelo="Voyage",
        km_compra=20_000,
        data_aquisicao=date(2026, 6, 10),  # mês anterior ao da leitura
    )
    registro = registrar(novo, date(2026, 7, 31), 23_000)
    assert registro.km_anterior == 20_000
    assert registro.km_utilizado == 3_000


# --- D21: leitura esquecida e leitura de fechamento no dia 1º -------------------


def test_d21_painel_cobra_tambem_o_mes_anterior_esquecido(veiculo):
    registrar(veiculo, date(2026, 8, 31), 15_000)
    pendencias = pendencias_de_leitura(date(2026, 10, 2))
    assert [(p.veiculo, p.mes, p.atrasada) for p in pendencias] == [
        (veiculo, date(2026, 9, 1), True),
        (veiculo, date(2026, 10, 1), False),
    ]


def test_d21_carro_sem_leitura_cobra_so_o_mes_atual(veiculo):
    pendencias = pendencias_de_leitura(date(2026, 10, 2))
    assert [(p.mes, p.atrasada) for p in pendencias] == [(date(2026, 10, 1), False)]


def test_d21_chip_do_painel_mostra_o_mes_esquecido(usuario_logado, veiculo):
    hoje = timezone.localdate()
    dois_meses_atras = (hoje.replace(day=1) - timedelta(days=40)).replace(day=15)
    registrar(veiculo, dois_meses_atras, 15_000)
    esquecido = (hoje.replace(day=1) - timedelta(days=1)).replace(day=1)

    conteudo = usuario_logado.get("/").content.decode()

    assert f"{esquecido:%m/%Y} esquecido" in conteudo
    assert f"?mes={esquecido:%Y-%m}#pendente-{veiculo.pk}" in conteudo


def test_d21_nos_primeiros_dias_a_tela_abre_no_mes_anterior(veiculo):
    registrar(veiculo, date(2026, 8, 31), 15_000)
    assert mes_padrao_da_lista(date(2026, 9, 3)) == date(2026, 9, 1)  # agosto já lido
    assert mes_padrao_da_lista(date(2026, 10, 3)) == date(2026, 9, 1)  # setembro esquecido
    assert mes_padrao_da_lista(date(2026, 10, 10)) == date(2026, 10, 1)  # passou do fechamento


def test_d21_leitura_de_fechamento_no_dia_1_vale_para_o_mes_da_tela(usuario_logado, veiculo):
    registrar(veiculo, date(2026, 7, 31), 15_000)
    usuario_logado.post(
        f"/km/registrar/{veiculo.pk}/",
        {"data_leitura": "2026-09-01", "km": "18000", "mes": "2026-08"},
    )
    agosto = RegistroKm.objects.get(veiculo=veiculo, data_leitura=date(2026, 9, 1))
    assert agosto.mes_referencia == date(2026, 8, 1)
    # a leitura do fim de setembro não é mais recusada como repetida
    usuario_logado.post(
        f"/km/registrar/{veiculo.pk}/",
        {"data_leitura": "2026-09-30", "km": "21000", "mes": "2026-09"},
    )
    setembro = RegistroKm.objects.get(veiculo=veiculo, mes_referencia=date(2026, 9, 1))
    assert setembro.km_anterior == 18_000
    assert setembro.dias == 29


def test_d21_leitura_no_meio_do_mes_seguinte_nao_fecha_o_mes_anterior(usuario_logado, veiculo):
    usuario_logado.post(
        f"/km/registrar/{veiculo.pk}/",
        {"data_leitura": "2026-09-10", "km": "13000", "mes": "2026-08"},
    )
    assert RegistroKm.objects.get(veiculo=veiculo).mes_referencia == date(2026, 9, 1)


def test_d21_corrigir_aceita_data_do_fechamento(veiculo):
    agosto = registrar(veiculo, date(2026, 9, 1), 18_000, mes=date(2026, 8, 1))
    form = CorrecaoLeituraForm(
        {"data_leitura": "2026-09-02", "km": "18100", "observacoes": ""}, instance=agosto
    )
    assert form.is_valid(), form.errors


def test_d21_data_fora_de_ordem_com_o_mes_seguinte_e_recusada(veiculo):
    registrar(veiculo, date(2026, 9, 2), 18_000)  # setembro
    agosto = RegistroKm(
        veiculo=veiculo, mes_referencia=date(2026, 8, 1), data_leitura=date(2026, 9, 3), km=17_000
    )
    with pytest.raises(Exception, match="antes da leitura de 09/2026"):
        agosto.full_clean()


# --- D22: franquia em meses de 28 e 31 dias -----------------------------------


@pytest.fixture
def contrato_desde_janeiro(veiculo):
    return alocar(veiculo, cliente("Arlen", "111.222.333-44"), date(2026, 1, 1), 12_000)


def test_d22_fevereiro_inteiro_usa_a_franquia_cheia(veiculo, contrato_desde_janeiro):
    registrar(veiculo, date(2026, 1, 31), 12_500)
    fevereiro = registrar(veiculo, date(2026, 2, 28), 15_450)  # 2.950 km em 28 dias
    assert gerar_cobrancas_excedente(fevereiro, hoje=date(2026, 2, 28)) == []


def test_d22_mes_de_31_dias_nao_ganha_franquia_a_mais(veiculo, contrato_desde_janeiro):
    registrar(veiculo, date(2026, 2, 28), 12_500)
    marco = registrar(veiculo, date(2026, 3, 31), 15_600)  # 3.100 km em 31 dias
    [cobranca] = gerar_cobrancas_excedente(marco, hoje=date(2026, 3, 31))
    assert cobranca.valor == Decimal("50.00")  # 100 km × R$ 0,50
    assert "franquia de 3000 km" in cobranca.descricao


def test_d22_mes_faltando_continua_rateado(veiculo, contrato_desde_janeiro):
    registrar(veiculo, date(2026, 1, 31), 12_500)
    marco = registrar(veiculo, date(2026, 3, 2), 18_000)  # fevereiro sem leitura, 30 dias
    [cobranca] = gerar_cobrancas_excedente(marco, hoje=date(2026, 3, 2))
    assert "franquia de 3000 km" in cobranca.descricao  # 3.000 × 30/30


# --- D30: mudar o limite do contrato não reavalia leitura antiga ---------------


def _contrato_ilimitado(veiculo):
    hoje = timezone.localdate()
    alocacao = alocar(
        veiculo,
        cliente("Arlen", "111.222.333-44"),
        hoje - timedelta(days=90),
        12_000,
        limite_km=Alocacao.LimiteKm.ILIMITADO,
        franquia_km_mensal=None,
        taxa_km_excedido=None,
    )
    registrar(veiculo, hoje.replace(day=1) - timedelta(days=1), 12_000)  # mês anterior
    return alocacao


def _passar_para_limitado(alocacao):
    alocacao.limite_km = Alocacao.LimiteKm.LIMITADO
    alocacao.franquia_km_mensal = 3_000
    alocacao.taxa_km_excedido = Decimal("0.50")
    alocacao.save()


def test_d30_mudanca_de_contrato_nao_cobra_leitura_feita_antes(veiculo):
    alocacao = _contrato_ilimitado(veiculo)
    hoje = timezone.localdate()
    registrar(veiculo, hoje, 16_200)  # 4.200 km com contrato ilimitado

    _passar_para_limitado(alocacao)

    assert gerar_excedentes_pendentes(hoje=hoje) == []


def test_d30_leitura_feita_depois_da_mudanca_usa_os_termos_novos(veiculo):
    alocacao = _contrato_ilimitado(veiculo)
    _passar_para_limitado(alocacao)
    hoje = timezone.localdate()
    registrar(veiculo, hoje, 16_200)

    [cobranca] = gerar_excedentes_pendentes(hoje=hoje)

    assert cobranca.cliente == alocacao.cliente
    assert cobranca.valor > 0


# --- Revisão: contrato cadastrado depois da leitura não duplica cobrança -------


def test_c15_contrato_cadastrado_depois_nao_cobra_duas_vezes_o_mesmo_cliente(veiculo):
    """A leitura cobrou Ana (era a única no sistema); depois cadastram o Bruno,
    que pegou o carro antes da leitura. A rotina não pode cobrar Ana de novo
    como 'acerto' — e o excedente do Bruno ainda precisa sair."""
    ana = cliente("Ana", "111.222.333-44")
    bruno = cliente("Bruno", "222.333.444-55")
    registrar(veiculo, date(2026, 6, 30), 12_000)
    alocacao_a = alocar(veiculo, ana, date(2026, 7, 1), 12_000)
    alocacao_a.encerrar(date(2026, 7, 10), 15_000)
    julho = registrar(veiculo, date(2026, 7, 31), 18_000)
    [principal] = gerar_cobrancas_excedente(julho, hoje=date(2026, 7, 31))
    assert principal.cliente == ana

    alocar(veiculo, bruno, date(2026, 7, 12), 15_000)  # cadastrado com atraso
    criadas = gerar_excedentes_pendentes(hoje=date(2026, 8, 1))

    assert [c.cliente.nome for c in criadas] == ["Bruno"]
    assert Cobranca.objects.filter(cliente=ana, origem=Cobranca.Origem.EXCEDENTE_KM).count() == 1
    assert gerar_excedentes_pendentes(hoje=date(2026, 8, 2)) == []
