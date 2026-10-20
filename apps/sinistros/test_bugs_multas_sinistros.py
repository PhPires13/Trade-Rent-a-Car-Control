"""Correções da frente multas-sinistros: D09, D16, D26, D33 (sinistro), C25."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.alocacoes.models import Alocacao
from apps.financeiro.models import Cobranca
from apps.financeiro.services import proximo_dia_pagamento
from apps.frota.models import Veiculo
from apps.manutencao.models import Manutencao
from apps.pessoas.models import Cliente
from apps.sinistros.models import AuxilioMotorista, Sinistro

HOJE = date.today()


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="GHI7J89", marca_modelo="HB20", km_atual=50000)


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Pedro", cpf_cnpj="111.222.333-44")


@pytest.fixture
def alocacao(veiculo, cliente):
    veiculo.status = Veiculo.Status.ALOCADO
    veiculo.save(update_fields=["status"])
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=60),
        valor_semanal=Decimal("650.00"),
        km_entrega=40000,
    )


def _dados(veiculo, **extra):
    dados = {
        "veiculo": veiculo.pk,
        "data": (HOJE - timedelta(days=3)).isoformat(),
        "cliente": "",
        "tipo": Sinistro.Tipo.COLISAO,
        "envolvido": Sinistro.Envolvido.ASSOCIADO,
        "responsabilidade": Sinistro.Responsabilidade.INDEFINIDA,
        "descricao": "",
        "boletim_ocorrencia": "",
        "responsavel_custo": "empresa",
        "status": Sinistro.Status.ABERTO,
        "observacoes": "",
    }
    dados.update(extra)
    return dados


def _manutencao(veiculo, sinistro, entrada, saida=None):
    return Manutencao.objects.create(
        veiculo=veiculo,
        tipo="corretiva",
        data=entrada,
        descricao="Conserto",
        data_entrada=entrada,
        data_saida=saida,
        sinistro=sinistro,
    )


# --- D09: franquia do motorista vira cobrança pelo botão "Cobrar do cliente" ---


def test_cobrar_franquia_cria_cobranca_para_o_motorista(usuario_logado, alocacao, veiculo, cliente):
    sinistro = Sinistro.objects.create(
        veiculo=veiculo,
        data=HOJE - timedelta(days=5),
        envolvido="associado",
        acionou_protecao=True,
        franquia_valor=Decimal("1500.00"),
        responsavel_custo="cliente",
    )
    assert sinistro.franquia_a_cobrar
    html = usuario_logado.get(f"/sinistros/{sinistro.pk}/editar/").content.decode()
    assert "Cobrar do cliente" in html
    url = f"/sinistros/{sinistro.pk}/cobrar-franquia/"
    usuario_logado.post(url, {"volta": f"/sinistros/{sinistro.pk}/editar/"})
    usuario_logado.post(url)  # clique duplo não cobra de novo
    cobrancas = Cobranca.objects.filter(origem=Cobranca.Origem.REPASSE_SINISTRO)
    assert cobrancas.count() == 1
    cobranca = cobrancas.get()
    assert cobranca.cliente == cliente
    assert cobranca.valor == Decimal("1500.00")
    assert cobranca.vencimento == proximo_dia_pagamento(cliente, HOJE)
    assert cobranca.vencimento > HOJE
    sinistro.refresh_from_db()
    assert sinistro.cobranca_franquia == cobranca
    assert not sinistro.franquia_a_cobrar


def test_franquia_da_empresa_nao_e_cobrada(usuario_logado, alocacao, veiculo):
    sinistro = Sinistro.objects.create(
        veiculo=veiculo,
        data=HOJE - timedelta(days=5),
        envolvido="associado",
        acionou_protecao=True,
        franquia_valor=Decimal("1500.00"),
        responsavel_custo="empresa",
    )
    usuario_logado.post(f"/sinistros/{sinistro.pk}/cobrar-franquia/")
    assert not Cobranca.objects.filter(origem=Cobranca.Origem.REPASSE_SINISTRO).exists()


# --- D16: roubo encerra a locação e deixa o carro Inativo até ser recuperado ---


def test_roubo_encerra_locacao_e_inativa_carro(usuario_logado, alocacao, veiculo):
    data_roubo = HOJE - timedelta(days=3)
    # semana com vencimento depois do roubo, ainda não paga: deixa de ser devida
    semana = Cobranca.objects.create(
        cliente=alocacao.cliente,
        alocacao=alocacao,
        origem=Cobranca.Origem.ALUGUEL,
        descricao="Aluguel semanal",
        valor=Decimal("650.00"),
        vencimento=HOJE,
    )
    resposta = usuario_logado.post(
        "/sinistros/novo/",
        _dados(veiculo, tipo=Sinistro.Tipo.ROUBO, data=data_roubo.isoformat(), token="t1"),
        follow=True,
    )
    assert "ficou Inativo" in resposta.content.decode()
    alocacao.refresh_from_db()
    veiculo.refresh_from_db()
    semana.refresh_from_db()
    assert alocacao.status == Alocacao.Status.ENCERRADA
    assert alocacao.data_termino == data_roubo
    assert alocacao.km_devolucao == 50000
    assert veiculo.status == Veiculo.Status.INATIVO
    assert semana.status == Cobranca.Status.CANCELADA

    sinistro = Sinistro.objects.get()
    html = usuario_logado.get(f"/sinistros/{sinistro.pk}/editar/").content.decode()
    assert "Carro recuperado" in html
    usuario_logado.post(f"/sinistros/{sinistro.pk}/recuperado/")
    veiculo.refresh_from_db()
    sinistro.refresh_from_db()
    assert veiculo.status == Veiculo.Status.DISPONIVEL
    assert sinistro.data_recuperacao == HOJE


def test_editar_para_roubo_tambem_encerra(usuario_logado, alocacao, veiculo):
    sinistro = Sinistro.objects.create(
        veiculo=veiculo, data=HOJE - timedelta(days=2), envolvido="associado"
    )
    usuario_logado.post(
        f"/sinistros/{sinistro.pk}/editar/",
        _dados(veiculo, tipo=Sinistro.Tipo.ROUBO, data=sinistro.data.isoformat()),
    )
    alocacao.refresh_from_db()
    veiculo.refresh_from_db()
    assert alocacao.status == Alocacao.Status.ENCERRADA
    assert veiculo.status == Veiculo.Status.INATIVO


def test_colisao_nao_mexe_na_locacao(usuario_logado, alocacao, veiculo):
    usuario_logado.post("/sinistros/novo/", _dados(veiculo, token="t2"))
    alocacao.refresh_from_db()
    veiculo.refresh_from_db()
    assert alocacao.status == Alocacao.Status.ATIVA
    assert veiculo.status == Veiculo.Status.ALOCADO


# --- D26: dias parados somados e auxílio só com a proteção acionada ---


def test_dias_parado_soma_todas_as_oficinas(alocacao, veiculo):
    sinistro = Sinistro.objects.create(
        veiculo=veiculo,
        data=HOJE - timedelta(days=30),
        envolvido="associado",
        acionou_protecao=True,
    )
    inicio = HOJE - timedelta(days=20)
    _manutencao(veiculo, sinistro, inicio, inicio + timedelta(days=5))  # funilaria
    _manutencao(
        veiculo, sinistro, inicio + timedelta(days=5), inicio + timedelta(days=10)
    )  # mecânica
    assert sinistro.dias_parado == 10
    assert sinistro.auxilio_disponivel is True


def test_dias_sobrepostos_nao_contam_duas_vezes(alocacao, veiculo):
    sinistro = Sinistro.objects.create(
        veiculo=veiculo, data=HOJE - timedelta(days=30), envolvido="associado"
    )
    inicio = HOJE - timedelta(days=20)
    _manutencao(veiculo, sinistro, inicio, inicio + timedelta(days=6))
    _manutencao(veiculo, sinistro, inicio + timedelta(days=3), inicio + timedelta(days=8))
    assert sinistro.dias_parado == 8


def test_auxilio_exige_protecao_acionada(usuario_logado, alocacao, veiculo):
    sinistro = Sinistro.objects.create(
        veiculo=veiculo, data=HOJE - timedelta(days=30), envolvido="associado"
    )
    _manutencao(veiculo, sinistro, HOJE - timedelta(days=12))
    assert sinistro.dias_parado == 12
    assert sinistro.auxilio_disponivel is False  # consertado por fora: sem evento
    usuario_logado.post(f"/sinistros/{sinistro.pk}/auxilio/", {"valor": "1.518,00"})
    assert not AuxilioMotorista.objects.exists()


# --- D33: reenviar "Novo sinistro" não grava em dobro ---


def test_novo_sinistro_reenviado_nao_duplica(usuario_logado, alocacao, veiculo):
    assert 'name="token"' in usuario_logado.get("/sinistros/novo/").content.decode()
    dados = _dados(veiculo, token="mesmo-codigo")
    usuario_logado.post("/sinistros/novo/", dados)
    resposta = usuario_logado.post("/sinistros/novo/", dados, follow=True)
    assert Sinistro.objects.count() == 1
    assert "envio repetido foi ignorado" in resposta.content.decode()


# --- C25: placa com hífen na busca ---


def test_filtro_de_placa_com_hifen_encontra_o_sinistro(usuario_logado, alocacao, veiculo):
    Sinistro.objects.create(
        veiculo=veiculo, data=HOJE, envolvido="associado", descricao="Batida no retrovisor"
    )
    html = usuario_logado.get("/sinistros/", {"veiculo": "GHI-7J89"}).content.decode()
    assert "Batida no retrovisor" in html


# --- revisão adversarial ---


def test_roubo_antigo_nao_encerra_a_locacao_de_agora(usuario_logado, alocacao, veiculo):
    """Carga de dados: roubo de antes da locação atual (carro já voltou e foi
    alugado de novo) não pode encerrar a locação nem inativar o carro."""
    data_roubo = alocacao.data_inicio - timedelta(days=30)
    resposta = usuario_logado.post(
        "/sinistros/novo/",
        _dados(veiculo, tipo=Sinistro.Tipo.ROUBO, data=data_roubo.isoformat(), token="t3"),
        follow=True,
    )
    assert "não foi encerrada sozinha" in resposta.content.decode()
    alocacao.refresh_from_db()
    veiculo.refresh_from_db()
    assert alocacao.status == Alocacao.Status.ATIVA
    assert veiculo.status == Veiculo.Status.ALOCADO
    assert Sinistro.objects.count() == 1


def test_franquia_com_cobranca_cancelada_pode_ser_cobrada_de_novo(
    usuario_logado, alocacao, veiculo, cliente
):
    sinistro = Sinistro.objects.create(
        veiculo=veiculo,
        data=HOJE - timedelta(days=5),
        envolvido="associado",
        acionou_protecao=True,
        franquia_valor=Decimal("1500.00"),
        responsavel_custo="cliente",
    )
    url = f"/sinistros/{sinistro.pk}/cobrar-franquia/"
    usuario_logado.post(url)
    primeira = Cobranca.objects.get(origem=Cobranca.Origem.REPASSE_SINISTRO)
    primeira.status = Cobranca.Status.CANCELADA
    primeira.save(update_fields=["status"])
    html = usuario_logado.get(f"/sinistros/{sinistro.pk}/editar/").content.decode()
    assert "foi cancelada no financeiro" in html
    assert "Cobrar do cliente" in html
    usuario_logado.post(url)
    sinistro.refresh_from_db()
    assert sinistro.cobranca_franquia != primeira
    assert sinistro.cobranca_franquia.status != Cobranca.Status.CANCELADA
    assert Cobranca.objects.filter(origem=Cobranca.Origem.REPASSE_SINISTRO).count() == 2
    usuario_logado.post(url)  # e não cobra uma terceira vez
    assert Cobranca.objects.filter(origem=Cobranca.Origem.REPASSE_SINISTRO).count() == 2


def test_roubo_de_carro_vendido_nao_diz_que_ficou_inativo(usuario_logado, veiculo):
    veiculo.status = Veiculo.Status.VENDIDO
    veiculo.save(update_fields=["status"])
    resposta = usuario_logado.post(
        "/sinistros/novo/",
        _dados(veiculo, tipo=Sinistro.Tipo.ROUBO, token="t4"),
        follow=True,
    )
    html = resposta.content.decode()
    assert "ficou Inativo" not in html
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.VENDIDO
