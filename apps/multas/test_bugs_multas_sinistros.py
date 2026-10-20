"""Correções da frente multas-sinistros: C04 (multas), C25, D23, D24, D33."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.alocacoes.models import Alocacao
from apps.financeiro.services import proximo_dia_pagamento
from apps.frota.models import Veiculo
from apps.multas import services
from apps.multas.models import Multa
from apps.pessoas.models import Cliente

HOJE = date.today()


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="ABC1D23", marca_modelo="Onix")


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="João", cpf_cnpj="111.222.333-44")


@pytest.fixture
def alocacao(veiculo, cliente):
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=100),
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
    )


def _multa(veiculo, **campos):
    campos.setdefault("data_infracao", HOJE - timedelta(days=20))
    campos.setdefault("valor", Decimal("195.23"))
    campos.setdefault("descricao", "Avançar sinal")
    return Multa.objects.create(veiculo=veiculo, **campos)


# --- C04: ND de multas vence no próximo dia de pagamento, não no dia da emissão ---


def test_nd_de_multas_sem_vencimento_vence_no_proximo_dia_de_pagamento(alocacao, veiculo, cliente):
    multa = _multa(veiculo)
    nd = services.gerar_nd_de_multas(cliente, [multa], HOJE)
    esperado = proximo_dia_pagamento(cliente, HOJE)
    assert esperado > HOJE
    assert nd.vencimento == esperado
    assert nd.cobranca.vencimento == esperado


# --- C25: busca por placa com hífen/espaço ---


def test_filtro_de_placa_com_hifen_encontra_a_multa(usuario_logado, alocacao, veiculo):
    _multa(veiculo, descricao="Excesso de velocidade")
    for digitado in ("ABC-1D23", "abc 1d23"):
        html = usuario_logado.get("/multas/", {"veiculo": digitado}).content.decode()
        assert "Excesso de velocidade" in html, digitado


# --- D23: multa paga pelo próprio cliente não é cobrada de novo ---


def test_multa_paga_pelo_cliente_nao_entra_na_nd(usuario_logado, alocacao, veiculo, cliente):
    paga = _multa(veiculo, descricao="Paga pelo João")
    outra = _multa(veiculo, descricao="Ainda a cobrar")
    resposta = usuario_logado.post(f"/multas/{paga.pk}/paga/", {"pago_por": "Cliente"}, follow=True)
    assert "não entra mais na nota de débito" in resposta.content.decode()
    paga.refresh_from_db()
    assert paga.repasse == "Não se aplica (pago pelo cliente)"
    # a tela de gerar ND só oferece a outra
    html = usuario_logado.get(f"/multas/gerar-nd/?cliente={cliente.pk}").content.decode()
    assert "Ainda a cobrar" in html
    assert "Paga pelo João" not in html
    # e o atalho "Cobrar do cliente" também não conta a paga
    contas = services.contar_tarefas(Multa.objects.all(), HOJE)
    assert contas["cobrar"] == 1
    assert list(services.filtrar_tarefa(Multa.objects.all(), "cobrar")) == [outra]
    # o serviço recusa se alguém tentar mesmo assim
    with pytest.raises(Exception, match="não está 'a cobrar'"):
        services.gerar_nd_de_multas(cliente, [paga], HOJE)


def test_multa_paga_pela_empresa_continua_a_cobrar(alocacao, veiculo):
    multa = _multa(veiculo, pagamento=Multa.Pagamento.PAGO, pago_por="Empresa")
    assert multa.repasse == "A cobrar"


# --- D24: FICI sem prazo aparece no alerta ---


def test_fici_sem_prazo_aparece_no_alerta_e_no_painel(usuario_logado, alocacao, veiculo):
    _multa(veiculo, descricao="Sem prazo", fici_prazo=None)
    _multa(veiculo, descricao="Prazo longe", fici_prazo=HOJE + timedelta(days=30))
    _multa(veiculo, descricao="Prazo perto", fici_prazo=HOJE + timedelta(days=2))
    alertas = [m.descricao for m in services.alertas_fici(HOJE)]
    assert alertas == ["Sem prazo", "Prazo perto"]
    html = usuario_logado.get("/").content.decode()
    assert "prazo não informado" in html


# --- D33: reenviar o mesmo formulário de "Nova multa" não grava em dobro ---


def test_nova_multa_reenviada_nao_duplica(usuario_logado, alocacao, veiculo):
    html = usuario_logado.get("/multas/nova/").content.decode()
    assert 'name="token"' in html
    dados = {
        "veiculo": veiculo.pk,
        "data_infracao": (HOJE - timedelta(days=5)).isoformat(),
        "cliente": "",
        "descricao": "Estacionar em local proibido",
        "valor": "195,23",
        "tipo_condutor": Multa.TipoCondutor.CLIENTE,
        "responsavel": Multa.Responsavel.CLIENTE,
        "token": "codigo-unico-1",
    }
    primeira = usuario_logado.post("/multas/nova/", dados)
    assert primeira.status_code == 302
    assert Multa.objects.count() == 1
    segunda = usuario_logado.post("/multas/nova/", dados, follow=True)
    assert Multa.objects.count() == 1
    assert "envio repetido foi ignorado" in segunda.content.decode()
    # um formulário novo (outro código) grava normalmente
    usuario_logado.post("/multas/nova/", {**dados, "token": "codigo-unico-2"})
    assert Multa.objects.count() == 2
