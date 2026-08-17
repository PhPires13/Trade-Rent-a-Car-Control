"""Regressões dos achados da revisão da etapa 8 no hub de clientes."""

from datetime import date
from decimal import Decimal

import pytest

from apps.alocacoes.models import Alocacao
from apps.financeiro import services as financeiro
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente
from apps.pessoas.views import _telefone_whatsapp


def test_whatsapp_nao_duplica_ddi():
    assert _telefone_whatsapp("(31) 98765-4321") == "5531987654321"
    assert _telefone_whatsapp("+55 (21) 98888-7777") == "5521988887777"  # já veio com DDI
    assert _telefone_whatsapp("") == ""


def test_lista_roda_com_queries_fixas(usuario_logado, django_assert_max_num_queries, db):
    """Saldos dos cards vêm em lote — não crescem com clientes/cobranças."""
    for indice in range(10):
        veiculo = Veiculo.objects.create(placa=f"TQ{indice:02d}B{indice:02d}", marca_modelo="Gol")
        cliente = Cliente.objects.create(
            nome=f"Cliente {indice}", cpf_cnpj=f"000.000.00{indice}-11"
        )
        Alocacao.objects.create(
            veiculo=veiculo,
            cliente=cliente,
            data_inicio=date(2026, 7, 1),
            valor_semanal=Decimal("650.00"),
            km_entrega=0,
        )
    financeiro.gerar_cobrancas_semanais(hoje=date(2026, 7, 15))  # 3 cobranças por cliente
    with django_assert_max_num_queries(10):
        resposta = usuario_logado.get("/clientes/")
    assert resposta.status_code == 200
    assert "R$" in resposta.content.decode()


def test_saldo_devedor_em_lote_bate_com_o_individual(usuario_logado, db):
    """O agregado do card reproduz Cobranca.saldo (com pagamento parcial)."""
    veiculo = Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol")
    cliente = Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")
    Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
    )
    financeiro.gerar_cobrancas_semanais(hoje=date(2026, 7, 8))  # 2 × 650
    cobranca = cliente.cobrancas.order_by("vencimento").first()
    financeiro.registrar_recebimento(
        cliente, date(2026, 7, 8), Decimal("200.00"), "pix", [(cobranca, Decimal("200.00"))]
    )
    resposta = usuario_logado.get("/clientes/")
    assert "1.100,00" in resposta.content.decode()  # 1300 − 200


@pytest.fixture
def cliente_com_carro_na_rua(db):
    veiculo = Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol")
    cliente = Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")
    alocacao = Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
    )
    return cliente, alocacao


def test_nao_inativa_cliente_com_alocacao_ativa(usuario_logado, cliente_com_carro_na_rua):
    """Inativo com carro na rua sumiria da lista e nunca viraria inadimplente."""
    cliente, _ = cliente_com_carro_na_rua
    resposta = usuario_logado.post(
        f"/clientes/{cliente.pk}/editar/",
        {"nome": cliente.nome, "cpf_cnpj": cliente.cpf_cnpj, "status": Cliente.Status.INATIVO},
    )
    assert resposta.status_code == 200  # volta com erro no campo status
    assert "Encerre a alocação" in resposta.content.decode()
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.ATIVO


def test_inativa_normalmente_depois_de_encerrar_a_alocacao(
    usuario_logado, cliente_com_carro_na_rua
):
    cliente, alocacao = cliente_com_carro_na_rua
    alocacao.encerrar(date(2026, 7, 20), km_devolucao=1_500)
    resposta = usuario_logado.post(
        f"/clientes/{cliente.pk}/editar/",
        {"nome": cliente.nome, "cpf_cnpj": cliente.cpf_cnpj, "status": Cliente.Status.INATIVO},
    )
    assert resposta.status_code == 302
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.INATIVO
