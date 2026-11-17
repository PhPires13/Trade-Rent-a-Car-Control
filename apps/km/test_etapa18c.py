"""Etapa 18c — sobras e refatoração da área de KM.

Corrigir/apagar leitura recalcula a inadimplência na hora e relê a cobrança
travada; franquia não rateada em dobro nos dias de troca; KM formatado na
retaguarda; rotina diária de excedente com pré-filtro dos carros limitados.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.contrib import admin
from django.utils import timezone

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.financeiro.models import Cobranca
from apps.financeiro.services import marcar_atrasos
from apps.frota.models import Veiculo
from apps.km.correcao import apagar_leitura, corrigir_leitura
from apps.km.excedente import gerar_cobranca_excedente, gerar_excedentes_pendentes
from apps.km.models import RegistroKm
from apps.pessoas.models import Cliente

HOJE = timezone.localdate()


def _leitura(veiculo, data, km):
    registro = RegistroKm(
        veiculo=veiculo, mes_referencia=data.replace(day=1), data_leitura=data, km=km
    )
    registro.full_clean()
    registro.save()
    return registro


def _alocar(veiculo, cliente, inicio, km_entrega, limite=Alocacao.LimiteKm.LIMITADO, **extra):
    dados = {"valor_semanal": Decimal("650.00"), "limite_km": limite}
    if limite == Alocacao.LimiteKm.LIMITADO:
        dados.update(franquia_km_mensal=3_000, taxa_km_excedido=Decimal("0.50"))
    dados.update(extra)
    return Alocacao.objects.create(
        veiculo=veiculo, cliente=cliente, data_inicio=inicio, km_entrega=km_entrega, **dados
    )


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_compra=50_000)


@pytest.fixture
def excedente_atrasado(veiculo, cliente):
    """Leitura de 20 dias atrás com excedente já vencido: cliente inadimplente."""
    _alocar(veiculo, cliente, HOJE - timedelta(days=60), 50_000)
    registro = _leitura(veiculo, HOJE - timedelta(days=20), 60_000)
    cobranca = gerar_cobranca_excedente(registro, hoje=registro.data_leitura)
    assert cobranca is not None and cobranca.vencimento < HOJE
    marcar_atrasos(hoje=HOJE)
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.INADIMPLENTE
    return registro


# --- (1) correção/apagar: inadimplência na hora e cobrança relida travada --------------


def test_corrigir_leitura_cancela_excedente_e_cliente_volta_a_ativo(excedente_atrasado, cliente):
    registro = RegistroKm.objects.get(pk=excedente_atrasado.pk)
    km_errado = registro.km
    registro.km = 50_500  # dentro da franquia: não há mais excedente
    corrigir_leitura(registro, km_errado)
    assert not Cobranca.objects.filter(
        cliente=cliente, status__in=Cobranca.STATUS_EM_ABERTO
    ).exists()
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.ATIVO


def test_apagar_leitura_cancela_excedente_e_cliente_volta_a_ativo(excedente_atrasado, cliente):
    apagar_leitura(RegistroKm.objects.get(pk=excedente_atrasado.pk))
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.ATIVO


def test_com_outra_divida_atrasada_continua_inadimplente(excedente_atrasado, cliente):
    Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.ALUGUEL,
        descricao="Aluguel atrasado",
        valor=Decimal("650.00"),
        vencimento=HOJE - timedelta(days=10),
        status=Cobranca.Status.ATRASADO,
    )
    apagar_leitura(RegistroKm.objects.get(pk=excedente_atrasado.pk))
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.INADIMPLENTE


def test_cliente_inativo_nao_muda(excedente_atrasado, cliente):
    Cliente.objects.filter(pk=cliente.pk).update(status=Cliente.Status.INATIVO)
    apagar_leitura(RegistroKm.objects.get(pk=excedente_atrasado.pk))
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.INATIVO


def test_corrigir_rele_a_cobranca_antes_de_cancelar(excedente_atrasado):
    """A leitura foi carregada antes; um "judicial" gravado noutra tela nesse meio
    tempo não pode ser desfeito pela correção."""
    registro = RegistroKm.objects.select_related("cobranca_excedente").get(pk=excedente_atrasado.pk)
    cobranca = registro.cobranca_excedente  # já em memória, ainda "Atrasado"
    Cobranca.objects.filter(pk=cobranca.pk).update(status=Cobranca.Status.JUDICIAL)
    km_errado = registro.km
    registro.km = 59_000
    avisos = corrigir_leitura(registro, km_errado)
    cobranca.refresh_from_db()
    assert cobranca.status == Cobranca.Status.JUDICIAL
    assert any("não foi mexida" in aviso for aviso in avisos)


# --- (2) franquia não rateada em dobro nos dias de troca ------------------------------


@pytest.fixture
def troca_no_meio(veiculo, cliente):
    """Contrato limitado desde 01/06; de 11/07 a 21/07 (10 dias) o cliente ficou
    com o substituto. Leituras do principal em 30/06 e 30/07 (30 dias)."""
    alocacao = _alocar(veiculo, cliente, date(2026, 6, 1), 50_000)
    _leitura(veiculo, date(2026, 6, 30), 53_000)
    substituto = Veiculo.objects.create(placa="RNB9J66", marca_modelo="Voyage", km_compra=19_000)
    TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=date(2026, 7, 11),
        km_retirada=20_000,
        data_devolucao=date(2026, 7, 21),
        km_devolucao=21_000,
    )
    return veiculo, substituto


def test_principal_desconta_os_dias_da_troca_da_franquia(troca_no_meio):
    veiculo, _substituto = troca_no_meio
    registro = _leitura(veiculo, date(2026, 7, 30), 56_500)  # 3.500 km no principal
    cobranca = gerar_cobranca_excedente(registro, hoje=date(2026, 7, 30))
    # mês completo = 3.000 km; 10 dias com o substituto já têm a franquia deles
    # (3.000 × 10/30 = 1.000 km) — no principal sobram 2.000 km: excedente 1.500 km
    assert cobranca is not None
    assert "1.500 km além da franquia de 2.000 km" in cobranca.descricao
    assert cobranca.valor == Decimal("750.00")


def test_franquia_somada_dos_dois_carros_e_a_do_mes(troca_no_meio):
    veiculo, substituto = troca_no_meio
    principal = _leitura(veiculo, date(2026, 7, 30), 56_500)
    leitura_sub = _leitura(substituto, date(2026, 7, 30), 21_000)
    cob_principal = gerar_cobranca_excedente(principal, hoje=date(2026, 7, 30))
    # substituto: 1.000 km em 10 dias contra 1.000 km de franquia — nada a cobrar
    assert gerar_cobranca_excedente(leitura_sub, hoje=date(2026, 7, 30)) is None
    assert "franquia de 2.000 km" in cob_principal.descricao  # 2.000 + 1.000 = 3.000


def test_sem_troca_no_periodo_franquia_cheia(veiculo, cliente):
    _alocar(veiculo, cliente, date(2026, 6, 1), 50_000)
    _leitura(veiculo, date(2026, 6, 30), 53_000)
    registro = _leitura(veiculo, date(2026, 7, 30), 56_500)
    cobranca = gerar_cobranca_excedente(registro, hoje=date(2026, 7, 30))
    assert "500 km além da franquia de 3.000 km" in cobranca.descricao


def test_troca_rateada_tambem_desconta(veiculo, cliente):
    """Período rateado (15 dias) com 5 dias de troca: franquia de 10 dias."""
    alocacao = _alocar(veiculo, cliente, date(2026, 6, 1), 50_000)
    _leitura(veiculo, date(2026, 6, 25), 52_000)
    substituto = Veiculo.objects.create(placa="RNB9J66", marca_modelo="Voyage", km_compra=19_000)
    TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=date(2026, 7, 1),
        km_retirada=20_000,
        data_devolucao=date(2026, 7, 6),
        km_devolucao=20_500,
    )
    registro = _leitura(veiculo, date(2026, 7, 10), 54_000)  # 15 dias, 2.000 km
    cobranca = gerar_cobranca_excedente(registro, hoje=date(2026, 7, 10))
    # 3.000 × 15/30 = 1.500, menos 3.000 × 5/30 = 500 → 1.000 km de franquia
    assert "1.000 km além da franquia de 1.000 km" in cobranca.descricao


# --- (4) retaguarda: KM formatado ---------------------------------------------------------


def test_admin_mostra_km_com_milhar(veiculo):
    from apps.manutencao.models import IntervaloPersonalizado, ItemPreventiva, Manutencao

    _leitura(veiculo, date(2026, 6, 10), 50_000)
    registro = _leitura(veiculo, date(2026, 7, 10), 54_000)
    modelo_admin = admin.site._registry[RegistroKm]
    colunas = {
        nome: getattr(modelo_admin, nome)(registro)
        for nome in modelo_admin.list_display
        if hasattr(modelo_admin, nome)
    }
    assert "54.000" in colunas.values()
    assert "50.000" in colunas.values()  # KM anterior

    item = ItemPreventiva.objects.create(nome="Óleo X", intervalo_km_padrao=10_000)
    manutencao = Manutencao.objects.create(
        veiculo=veiculo, item=item, tipo="preventiva", data=date(2026, 7, 1), km=53_000
    )
    personalizado = IntervaloPersonalizado.objects.create(
        veiculo=veiculo, item=item, intervalo_km=15_000
    )
    for modelo, objeto, esperado in [
        (Manutencao, manutencao, "53.000"),
        (ItemPreventiva, item, "10.000"),
        (IntervaloPersonalizado, personalizado, "15.000"),
    ]:
        modelo_admin = admin.site._registry[modelo]
        valores = [
            getattr(modelo_admin, nome)(objeto)
            for nome in modelo_admin.list_display
            if hasattr(modelo_admin, nome)
        ]
        assert esperado in valores, modelo


# --- refatoração: rotina diária com pré-filtro dos carros limitados ------------------------


def _cobrancas():
    return sorted(
        Cobranca.objects.filter(origem=Cobranca.Origem.EXCEDENTE_KM).values_list(
            "cliente__nome", "valor", "descricao"
        )
    )


@pytest.fixture
def frota_mista(db):
    """Limitado, limitado que virou ilimitado depois da leitura (D30), limitado só
    como substituto e uma frota de ilimitados sem nada a cobrar."""
    hoje = HOJE
    inicio = hoje - timedelta(days=40)
    clientes = [Cliente.objects.create(nome=f"Cliente {i}", cpf_cnpj=f"mix-{i}") for i in range(12)]
    carros = [
        Veiculo.objects.create(placa=f"MIX{i:04d}", marca_modelo="Gol", km_compra=10_000)
        for i in range(12)
    ]
    # limitado comum
    _alocar(carros[0], clientes[0], inicio, 10_000)
    _leitura(carros[0], hoje - timedelta(days=5), 16_000)
    # limitado → ilimitado depois da leitura (vale o contrato da hora da leitura)
    d30 = _alocar(carros[1], clientes[1], inicio, 10_000)
    _leitura(carros[1], hoje - timedelta(days=5), 16_000)
    d30.limite_km = Alocacao.LimiteKm.ILIMITADO
    d30.franquia_km_mensal = None
    d30.taxa_km_excedido = None
    d30.save()
    # limitado só como substituto (o principal é o carro 2)
    alocacao = _alocar(carros[2], clientes[2], inicio, 10_000)
    TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=carros[3],
        data_retirada=inicio + timedelta(days=1),
        km_retirada=10_000,
    )
    _leitura(carros[3], hoje - timedelta(days=5), 16_000)
    # ilimitados
    for carro, pessoa in zip(carros[4:], clientes[4:], strict=True):
        _alocar(carro, pessoa, inicio, 10_000, limite=Alocacao.LimiteKm.ILIMITADO)
        _leitura(carro, hoje - timedelta(days=5), 16_000)
    return carros


def test_rotina_diaria_cobra_o_mesmo_no_cenario_misto(frota_mista):
    criadas = gerar_excedentes_pendentes()
    assert {c.cliente.nome for c in criadas} == {"Cliente 0", "Cliente 1", "Cliente 2"}
    # roda de novo: idempotente
    assert gerar_excedentes_pendentes() == []
    assert len(_cobrancas()) == 3


def test_rotina_diaria_com_frota_ilimitada_tem_consultas_constantes(
    django_assert_max_num_queries, db
):
    inicio = HOJE - timedelta(days=40)
    for i in range(30):
        carro = Veiculo.objects.create(placa=f"ILI{i:04d}", marca_modelo="Gol", km_compra=10_000)
        pessoa = Cliente.objects.create(nome=f"Livre {i}", cpf_cnpj=f"livre-{i}")
        _alocar(carro, pessoa, inicio, 10_000, limite=Alocacao.LimiteKm.ILIMITADO)
        _leitura(carro, HOJE - timedelta(days=5), 16_000)
    with django_assert_max_num_queries(8):
        assert gerar_excedentes_pendentes() == []


def test_rotina_diaria_uma_leitura_com_erro_nao_impede_as_outras(frota_mista, monkeypatch):
    from apps.financeiro.services import FalhaEmAlguns
    from apps.km import excedente

    original = excedente.gerar_cobrancas_excedente
    quebrado = frota_mista[0]

    def talvez_quebre(registro, hoje=None):
        if registro.veiculo_id == quebrado.pk:
            raise RuntimeError("dado ruim")
        return original(registro, hoje=hoje)

    monkeypatch.setattr(excedente, "gerar_cobrancas_excedente", talvez_quebre)
    with pytest.raises(FalhaEmAlguns) as erro:
        gerar_excedentes_pendentes()
    assert {c.cliente.nome for c in erro.value.criadas} == {"Cliente 1", "Cliente 2"}


# --- revisão: mês completo com troca soma a franquia do mês (D22), em qualquer mês -------


@pytest.mark.parametrize(
    ("leitura_anterior", "leitura", "troca", "esperado"),
    [
        # fevereiro (28 dias): ficavam 3.000 × 18/28 = 1.929 km no principal — o mês
        # completo com troca dava 2.929 km, menos que a franquia cheia (D22)
        (
            date(2026, 1, 31),
            date(2026, 2, 28),
            (date(2026, 2, 5), date(2026, 2, 15)),
            "1.000 km além da franquia de 2.000 km",
        ),
        # mês de 31 dias: a proporção dos dias do cliente no carro (21/31) é o piso —
        # sobra um pouco para o km da oficina, que não se separa da leitura
        (
            date(2026, 6, 30),
            date(2026, 7, 31),
            (date(2026, 7, 11), date(2026, 7, 21)),
            "968 km além da franquia de 2.032 km",
        ),
    ],
)
def test_mes_completo_com_troca_principal_mais_substituto_da_a_franquia_cheia(
    veiculo, cliente, leitura_anterior, leitura, troca, esperado
):
    alocacao = _alocar(veiculo, cliente, leitura_anterior - timedelta(days=30), 50_000)
    _leitura(veiculo, leitura_anterior, 53_000)
    substituto = Veiculo.objects.create(placa="RNB9J66", marca_modelo="Voyage", km_compra=19_000)
    TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=troca[0],
        km_retirada=20_000,
        data_devolucao=troca[1],
        km_devolucao=21_500,
    )
    principal = gerar_cobranca_excedente(_leitura(veiculo, leitura, 56_000), hoje=leitura)
    no_substituto = gerar_cobranca_excedente(_leitura(substituto, leitura, 21_500), hoje=leitura)
    # 10 dias no substituto = 1.000 km de franquia; o principal fica com o resto do
    # mês completo (3.000 − 1.000), nunca menos que a proporção dos dias dele
    assert esperado in principal.descricao
    assert "500 km além da franquia de 1.000 km" in no_substituto.descricao


# --- KM do mês: depois de registrar, "Leituras do mês" se atualiza (htmx oob) ------


def test_registrar_pelo_htmx_atualiza_leituras_do_mes(usuario_logado, db):
    veiculo = Veiculo.objects.create(placa="SEM1L09", marca_modelo="Gol", km_atual=100_000)
    Veiculo.objects.create(placa="SEM1L10", marca_modelo="Gol", km_atual=50_000)
    pagina = usuario_logado.get("/km/").content.decode()
    assert 'id="leituras-do-mes"' in pagina
    assert "Leituras do mês (0)" in pagina

    resposta = usuario_logado.post(
        f"/km/registrar/{veiculo.pk}/",
        {"data_leitura": HOJE.isoformat(), "km": "101234", "mes": f"{HOJE:%Y-%m}"},
        HTTP_HX_REQUEST="true",
    )

    html = resposta.content.decode()
    assert resposta.status_code == 200
    # o bloco de baixo volta junto, trocado fora da linha (oob)
    assert '<section id="leituras-do-mes" hx-swap-oob="true">' in html
    assert "Leituras do mês (1)" in html
    assert "Nenhuma leitura registrada" not in html
    assert "101.234" in html
    # o contador de pendentes continua vindo
    assert 'id="contador-pendentes" hx-swap-oob="true"' in html


def test_pagina_sem_leituras_continua_dizendo_nenhuma(usuario_logado, db):
    Veiculo.objects.create(placa="SEM1L11", marca_modelo="Gol", km_atual=100_000)
    pagina = usuario_logado.get("/km/").content.decode()
    assert "Nenhuma leitura registrada neste mês — use a lista de pendentes acima." in pagina
    assert '<section id="leituras-do-mes">' in pagina  # na página, sem oob
