"""Pendências da frente multas-sinistros: "Quem pagou" é escolha, não texto livre (D23)."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.alocacoes.models import Alocacao
from apps.frota.models import Veiculo
from apps.multas.models import Multa
from apps.pessoas.models import Cliente

HOJE = date.today()


@pytest.fixture
def veiculo(db):
    cliente = Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")
    veiculo = Veiculo.objects.create(placa="QXQ6C11", marca_modelo="Gol")
    Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=200),
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
    )
    return veiculo


def _multa(veiculo, **campos):
    campos.setdefault("data_infracao", HOJE - timedelta(days=30))
    campos.setdefault("valor", Decimal("100.00"))
    return Multa.objects.create(veiculo=veiculo, **campos)


def _dados_form(multa, **extra):
    dados = {
        "veiculo": multa.veiculo_id,
        "data_infracao": multa.data_infracao.isoformat(),
        "cliente": multa.cliente_id or "",
        "descricao": multa.descricao,
        "valor": "100,00",
        "tipo_condutor": multa.tipo_condutor,
        "responsavel": multa.responsavel,
        "resultado": multa.resultado,
        "fici_status": multa.fici_status,
        "pagamento": multa.pagamento,
        "pago_por": multa.pago_por,
    }
    dados.update(extra)
    return dados


def test_quem_pagou_na_edicao_e_uma_lista_de_opcoes(usuario_logado, veiculo):
    multa = _multa(veiculo)
    html = usuario_logado.get(f"/multas/{multa.pk}/editar/").content.decode()
    assert '<select name="pago_por"' in html
    assert 'value="Cliente"' in html and 'value="Empresa"' in html
    assert "opcoes-pago-por" not in html  # o texto livre com sugestões saiu


def test_texto_solto_em_quem_pagou_e_recusado(usuario_logado, veiculo):
    multa = _multa(veiculo)
    resposta = usuario_logado.post(
        f"/multas/{multa.pk}/editar/",
        _dados_form(multa, pagamento="pago", pago_por="João (motorista)"),
    )
    assert resposta.status_code == 200  # volta para a tela com erro
    multa.refresh_from_db()
    assert multa.pagamento == Multa.Pagamento.PENDENTE


def test_escolher_cliente_tira_a_multa_da_nd(usuario_logado, veiculo):
    multa = _multa(veiculo)
    usuario_logado.post(
        f"/multas/{multa.pk}/editar/", _dados_form(multa, pagamento="pago", pago_por="Cliente")
    )
    multa.refresh_from_db()
    assert multa.pago_por == "Cliente"
    assert multa.paga_pelo_cliente


def test_texto_antigo_continua_na_lista_para_nao_ser_apagado(usuario_logado, veiculo):
    multa = _multa(veiculo, pagamento=Multa.Pagamento.PAGO, pago_por="João (motorista)")
    html = usuario_logado.get(f"/multas/{multa.pk}/editar/").content.decode()
    assert "João (motorista) (anotado antes)" in html
    usuario_logado.post(f"/multas/{multa.pk}/editar/", _dados_form(multa, observacoes="ok"))
    multa.refresh_from_db()
    assert multa.pago_por == "João (motorista)"


def test_cliente_minusculo_gravado_antes_vira_a_opcao_cliente(usuario_logado, veiculo):
    multa = _multa(veiculo, pagamento=Multa.Pagamento.PAGO, pago_por="cliente")
    html = usuario_logado.get(f"/multas/{multa.pk}/editar/").content.decode()
    assert '<option value="Cliente" selected>' in html


def test_marcar_paga_so_aceita_empresa_ou_cliente(usuario_logado, veiculo):
    multa = _multa(veiculo)
    usuario_logado.post(f"/multas/{multa.pk}/paga/", {"pago_por": "cliente"})
    multa.refresh_from_db()
    assert multa.pago_por == "Cliente"
    outra = _multa(veiculo, ait="X2")
    usuario_logado.post(f"/multas/{outra.pk}/paga/", {"pago_por": "<b>qualquer</b>"})
    outra.refresh_from_db()
    assert outra.pagamento == Multa.Pagamento.PAGO
    assert outra.pago_por == ""


def test_nic_repetida_com_tela_antiga_avisa_em_vez_de_erro(veiculo):
    from apps.multas import services

    multa = _multa(veiculo, fici_prazo=HOJE - timedelta(days=1))
    antiga = Multa.objects.get(pk=multa.pk)  # outra aba, carregada antes da 1ª NIC
    services.registrar_nic(multa, valor=Decimal("130.16"))
    with pytest.raises(ValidationError, match="já tem NIC"):
        services.registrar_nic(antiga, valor=Decimal("130.16"))
    assert Multa.objects.filter(multa_origem_nic=multa).count() == 1
