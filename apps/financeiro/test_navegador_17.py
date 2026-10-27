"""Etapa 17 (navegador): achados do teste no navegador — cada correção com teste."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.financeiro import services, whatsapp
from apps.financeiro.devedores import devedores
from apps.financeiro.models import (
    Cobranca,
    MovimentacaoCaucao,
    MovimentoCredito,
    NotaDebito,
    Recebimento,
)
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente

HOJE = timezone.localdate()
URL_RECEBER = "/financeiro/receber/"


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(
        nome="Ana Paula", cpf_cnpj="111.222.333-77", telefone="31988887777"
    )


@pytest.fixture
def alocacao(cliente):
    veiculo = Veiculo.objects.create(placa="RNV1A17", marca_modelo="Gol")
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=120),
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
        caucao_valor=Decimal("1000.00"),
    )


def _cobranca(cliente, alocacao=None, dias_atras=10, valor="650.00", origem=None):
    cobranca = Cobranca.objects.create(
        cliente=cliente,
        alocacao=alocacao,
        origem=origem or Cobranca.Origem.ALUGUEL,
        descricao=f"Aluguel {dias_atras}",
        valor=Decimal(valor),
        vencimento=HOJE - timedelta(days=dias_atras),
    )
    cobranca.atualizar_status()
    return cobranca


def _post_receber(client, cliente, **campos):
    dados = {"cliente": cliente.pk, "data": HOJE.isoformat(), "forma": "pix"}
    dados.update(campos)
    return client.post(URL_RECEBER, dados)


def _mensagens(resposta):
    return [str(m) for m in resposta.context["messages"]]


# --- data de pagamento do mês anterior pede confirmação (DAS pode estar fechado) ---


def test_pagamento_com_data_do_mes_anterior_pede_confirmacao(cliente):
    ultimo_dia_mes_passado = services.inicio_do_mes(HOJE) - timedelta(days=1)
    with pytest.raises(ValidationError, match="Confirmo: pagamento de mês passado"):
        services.validar_data_pagamento(ultimo_dia_mes_passado, HOJE, mes_passado_confirmado=False)
    # dia 1º do mês atual não pede nada
    services.validar_data_pagamento(services.inicio_do_mes(HOJE), HOJE, False)


def test_receber_com_data_do_mes_anterior_sem_marcar_nao_lanca(usuario_logado, cliente):
    semana = _cobranca(cliente, dias_atras=40)
    data = (services.inicio_do_mes(HOJE) - timedelta(days=1)).isoformat()
    dados = {"valor": "650,00", "data": data, f"aplicar_{semana.pk}": "650,00"}
    resposta = _post_receber(usuario_logado, cliente, **dados)
    assert resposta.status_code == 200
    assert any("Confirmo: pagamento de mês passado" in m for m in _mensagens(resposta))
    assert not Recebimento.objects.exists()
    resposta = _post_receber(usuario_logado, cliente, confirmar_mes_passado="1", **dados)
    assert resposta.status_code == 302


# --- valor com mais de 2 casas decimais não é arredondado em silêncio ------------


def test_valor_com_mais_de_2_casas_e_recusado(usuario_logado, cliente):
    semana = _cobranca(cliente)
    resposta = _post_receber(usuario_logado, cliente, valor="1.000,0010000")
    assert any("2 casas" in m for m in _mensagens(resposta))
    assert not Recebimento.objects.exists()
    # zeros a mais não mudam o valor: continua aceito
    resposta = _post_receber(
        usuario_logado, cliente, valor="650,0000", **{f"aplicar_{semana.pk}": "650,00"}
    )
    assert resposta.status_code == 302
    assert Recebimento.objects.get().valor == Decimal("650.00")


def test_receber_tem_aviso_de_casas_decimais_no_navegador(usuario_logado, cliente):
    _cobranca(cliente)
    html = usuario_logado.get(f"{URL_RECEBER}?cliente={cliente.pk}").content.decode()
    assert "casasDemais" in html and "use só 2 casas" in html


# --- Admin: cobrança, ND e recebimento não se apagam ------------------------------


@pytest.mark.parametrize("modelo", ["cobranca", "notadebito", "recebimento"])
def test_admin_nao_apaga_lancamento_financeiro(admin_client, cliente, modelo):
    cobranca = _cobranca(cliente)
    if modelo == "cobranca":
        pk = cobranca.pk
    elif modelo == "notadebito":
        pk = services.emitir_nota_debito(cliente, HOJE, [("Avaria", Decimal("300.00"))]).pk
    else:
        pk = services.registrar_recebimento(
            cliente, HOJE, Decimal("100.00"), "pix", [(cobranca, Decimal("100.00"))]
        ).pk
    assert admin_client.get(f"/admin/financeiro/{modelo}/{pk}/delete/").status_code == 403
    lista = admin_client.get(f"/admin/financeiro/{modelo}/")
    assert "delete_selected" not in lista.content.decode()
    # o aviso "Só consulta" não vira o título da aba
    titulo = lista.content.decode().split("<title>")[1].split("</title>")[0]
    assert "Só consulta" not in titulo
    assert "Só consulta" in lista.content.decode()


def test_admin_cobrancas_mais_recentes_primeiro(admin_client, cliente):
    _cobranca(cliente, dias_atras=30)
    nova = _cobranca(cliente, dias_atras=1)
    resposta = admin_client.get("/admin/financeiro/cobranca/")
    assert list(resposta.context["cl"].result_list)[0] == nova


# --- "deve" igual em todas as telas: vencido + a vencer à parte -------------------


def test_seletor_mostra_deve_hoje_e_a_vencer_separados(usuario_logado, cliente):
    _cobranca(cliente, dias_atras=5, valor="320.00")
    _cobranca(cliente, dias_atras=-3, valor="180.00")
    so_a_vencer = Cliente.objects.create(nome="Bruno", cpf_cnpj="222.333.444-55")
    _cobranca(so_a_vencer, dias_atras=-2, valor="650.00")
    html = usuario_logado.get(URL_RECEBER).content.decode()
    assert "deve hoje R$ 320,00 (+ R$ 180,00 a vencer)" in html
    assert "Bruno — R$ 650,00 a vencer" in html
    assert "deve R$ 500,00" not in html


def test_continua_inadimplente_mostra_so_o_vencido(usuario_logado, cliente):
    atrasada = _cobranca(cliente, dias_atras=10, valor="650.00")
    _cobranca(cliente, dias_atras=10, valor="320.00")
    _cobranca(cliente, dias_atras=-3, valor="650.00")
    services.atualizar_inadimplencia(cliente)
    resposta = _post_receber(
        usuario_logado, cliente, valor="650,00", **{f"aplicar_{atrasada.pk}": "650,00"}
    )
    html = usuario_logado.get(resposta["Location"]).content.decode()
    assert "continua inadimplente: deve hoje R$ 320,00 (+ R$ 650,00 a vencer)" in html


# --- crédito guardado não some na cobrança da ND -----------------------------------


def test_nd_com_credito_avisa_na_mensagem_e_na_lista(usuario_logado, cliente):
    nd = services.emitir_nota_debito(cliente, HOJE, [("Avaria", Decimal("300.00"))])
    MovimentoCredito.objects.create(
        cliente=cliente, tipo=MovimentoCredito.Tipo.ENTRADA, valor=Decimal("50.00"), data=HOJE
    )
    texto = whatsapp.mensagem_nd(NotaDebito.objects.get(pk=nd.pk), HOJE, Decimal("50.00"))
    assert "R$ 50,00 de crédito guardado" in texto
    html = usuario_logado.get("/financeiro/nd/").content.decode()
    assert "R$ 50,00 de crédito" in html


def test_receber_avisa_credito_antes_de_lancar_dinheiro_novo(usuario_logado, cliente):
    semana = _cobranca(cliente)
    MovimentoCredito.objects.create(
        cliente=cliente, tipo=MovimentoCredito.Tipo.ENTRADA, valor=Decimal("50.00"), data=HOJE
    )
    html = usuario_logado.get(
        f"{URL_RECEBER}?cliente={cliente.pk}&cobranca={semana.pk}"
    ).content.decode()
    assert "data-aviso-credito" in html and "usar antes?" in html


# --- erro na ação do painel não pula para a linha (mensagem fica visível) ---------


def test_encargo_recusado_volta_ao_topo_sem_ancora(usuario_logado, cliente):
    atrasada = _cobranca(cliente, dias_atras=10)
    resposta = usuario_logado.post(
        f"/financeiro/cobranca/{atrasada.pk}/encargo/", {"valor": "650,00"}
    )
    assert resposta.status_code == 302
    assert "#cobranca-" not in resposta["Location"]
    resposta = usuario_logado.post(
        f"/financeiro/cobranca/{atrasada.pk}/encargo/", {"valor": "32,50"}
    )
    assert resposta["Location"].endswith(f"#cobranca-{atrasada.pk}")


# --- desfazer pelo extrato que falha volta ao extrato ------------------------------


def test_desfazer_pelo_extrato_que_falha_volta_ao_extrato(usuario_logado, cliente):
    primeira = _cobranca(cliente, dias_atras=10, valor="100.00")
    segunda = _cobranca(cliente, dias_atras=5, valor="100.00")
    com_sobra = services.registrar_recebimento(
        cliente, HOJE, Decimal("200.00"), "pix", [(primeira, Decimal("100.00"))]
    )
    services.registrar_recebimento(
        cliente, HOJE, Decimal("100.00"), "credito", [(segunda, Decimal("100.00"))]
    )
    extrato = f"/financeiro/cliente/{cliente.pk}/"
    resposta = usuario_logado.post(
        f"/financeiro/recebimento/{com_sobra.pk}/desfazer/", {"voltar": "extrato"}
    )
    assert resposta["Location"] == extrato
    pagina = usuario_logado.get(extrato).content.decode()
    assert "já foi usado" in pagina and "Abatimento com crédito do cliente" in pagina
    assert Recebimento.objects.filter(pk=com_sobra.pk).exists()


def test_desfazer_pelo_extrato_que_da_certo_volta_ao_extrato(usuario_logado, cliente):
    semana = _cobranca(cliente)
    recebimento = services.registrar_recebimento(
        cliente, HOJE, Decimal("650.00"), "pix", [(semana, Decimal("650.00"))]
    )
    resposta = usuario_logado.post(
        f"/financeiro/recebimento/{recebimento.pk}/desfazer/", {"voltar": "extrato"}
    )
    assert resposta["Location"] == f"/financeiro/cliente/{cliente.pk}/"
    assert not Recebimento.objects.exists()


# --- textos e telas ------------------------------------------------------------


def test_cobranca_cancelada_nao_mostra_falta_receber(usuario_logado, cliente):
    excedente = _cobranca(cliente, origem=Cobranca.Origem.EXCEDENTE_KM, valor="123.45")
    excedente.status = Cobranca.Status.CANCELADA
    excedente.save(update_fields=["status"])
    html = usuario_logado.get("/financeiro/?status=cancelada").content.decode()
    assert "não cobrar" in html and "<s" in html


def test_receber_acima_do_saldo_explica_em_portugues(usuario_logado, cliente):
    semana = _cobranca(cliente, valor="650.00")
    resposta = _post_receber(
        usuario_logado, cliente, valor="700,00", **{f"aplicar_{semana.pk}": "700,00"}
    )
    assert any("só faltam R$ 650,00" in m for m in _mensagens(resposta))


def test_sobra_sem_caucao_vira_texto_fixo(usuario_logado, cliente):
    _cobranca(cliente)
    html = usuario_logado.get(f"{URL_RECEBER}?cliente={cliente.pk}").content.decode()
    assert '<input type="hidden" name="sobra_destino" id="sobra_destino" value="credito">' in html
    assert '<select name="sobra_destino"' not in html


def test_judicial_sem_encargo_automatico_e_avisa_carro_com_o_cliente(
    usuario_logado, cliente, alocacao
):
    _cobranca(cliente, alocacao)
    html = usuario_logado.get("/financeiro/").content.decode()
    assert "encargo automático" not in html
    assert "A cobrança sai das listas de atraso" in html
    assert "o carro RNV1A17 ainda está com este cliente" in html


def test_telefone_incompleto_nao_diz_sem_telefone(usuario_logado, cliente):
    cliente.telefone = "98888-7777"
    cliente.save(update_fields=["telefone"])
    semana = _cobranca(cliente)
    resposta = _post_receber(
        usuario_logado, cliente, valor="650,00", **{f"aplicar_{semana.pk}": "650,00"}
    )
    html = usuario_logado.get(resposta["Location"]).content.decode()
    assert "Telefone incompleto no cadastro" in html


def test_quem_deve_mostra_ex_placa_de_ex_cliente(cliente, alocacao):
    _cobranca(cliente, alocacao)
    Alocacao.objects.filter(pk=alocacao.pk).update(status=Alocacao.Status.ENCERRADA)
    assert devedores(HOJE)[0].placas == ["ex-RNV1A17"]


# --- caução --------------------------------------------------------------------


def _deposito(alocacao, valor="1000.00"):
    services.registrar_movimentacao_caucao(
        alocacao.caucao,
        MovimentacaoCaucao.Tipo.RECEBIMENTO,
        Decimal(valor),
        alocacao.data_inicio,
        forma="pix",
    )
    return alocacao.caucao


def test_devolucao_parcial_nao_e_caucao_utilizada(alocacao):
    caucao = _deposito(alocacao)
    services.registrar_movimentacao_caucao(
        caucao,
        MovimentacaoCaucao.Tipo.DEVOLUCAO,
        Decimal("100.00"),
        HOJE,
        devolver_mesmo_assim=True,
    )
    assert caucao.status == "Parcialmente devolvida"


def test_caucao_depois_de_encerrar_abre_no_acerto(usuario_logado, cliente, alocacao):
    caucao = _deposito(alocacao)
    _cobranca(cliente, alocacao, valor="300.00")
    Alocacao.objects.filter(pk=alocacao.pk).update(status=Alocacao.Status.ENCERRADA)
    resposta = usuario_logado.get(f"/financeiro/caucoes/{caucao.pk}/")
    assert resposta.context["tipo_inicial"] == MovimentacaoCaucao.Tipo.DESCONTO
    assert resposta.context["valor_inicial"] == "300,00"
    Cobranca.objects.update(status=Cobranca.Status.CANCELADA)
    resposta = usuario_logado.get(f"/financeiro/caucoes/{caucao.pk}/")
    assert resposta.context["tipo_inicial"] == MovimentacaoCaucao.Tipo.DEVOLUCAO
    assert resposta.context["valor_inicial"] == "1.000,00"


def test_caucao_com_carro_ativo_abre_no_deposito(usuario_logado, alocacao):
    resposta = usuario_logado.get(f"/financeiro/caucoes/{alocacao.caucao.pk}/")
    assert resposta.context["tipo_inicial"] == MovimentacaoCaucao.Tipo.RECEBIMENTO
    assert "acimaDoSaldo" in resposta.content.decode()
