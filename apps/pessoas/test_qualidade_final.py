"""Qualidade final — clientes: cadastro, ficha (celular, caução, oficina), lista e condutores."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.alocacoes.models import Alocacao
from apps.financeiro.models import Cobranca
from apps.frota.models import Veiculo
from apps.manutencao.models import Manutencao
from apps.pessoas.models import Cliente, CondutorAutorizado
from apps.pessoas.views import ClienteForm

HOJE = date.today()


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(
        nome="Bruno Lima", cpf_cnpj="222.333.444-55", telefone="31988887777"
    )


@pytest.fixture
def alocacao(cliente):
    veiculo = Veiculo.objects.create(placa="RTD4E07", marca_modelo="Onix", km_atual=1_000)
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=3),
        valor_semanal=Decimal("700.00"),
        dia_vencimento=5,
        caucao_valor=Decimal("1000.00"),
        km_entrega=1_000,
    )


def test_cadastro_novo_nao_mostra_status():
    assert "status" not in ClienteForm().fields


def test_edicao_continua_com_status(cliente):
    assert "status" in ClienteForm(instance=cliente).fields


def test_dia_de_vencimento_vazio_tem_rotulo_claro():
    escolhas = dict(ClienteForm().fields["dia_vencimento"].choices)
    assert escolhas[""] == "Mesmo dia em que pegar o carro"
    assert "Caução padrão" in ClienteForm().fields["caucao_referencia"].label


def test_ficha_mostra_dia_da_alocacao_e_caucao_nao_depositada(usuario_logado, alocacao):
    html = usuario_logado.get(f"/clientes/{alocacao.cliente_id}/").content.decode()
    assert "o da alocação: Sábado" in html
    assert "R$ 1.000,00" in html and "ainda não depositada" in html


def test_ficha_avisa_carro_na_oficina(usuario_logado, alocacao):
    Manutencao.objects.create(
        veiculo=alocacao.veiculo,
        tipo=Manutencao.Tipo.CORRETIVA,
        data=HOJE - timedelta(days=1),
        data_entrada=HOJE - timedelta(days=1),
    )
    html = usuario_logado.get(f"/clientes/{alocacao.cliente_id}/").content.decode()
    assert "Carro na oficina desde" in html
    assert "Emprestar um substituto" in html


def test_ficha_sem_grade_que_estoura_no_celular(usuario_logado, cliente):
    html = usuario_logado.get(f"/clientes/{cliente.pk}/").content.decode()
    assert "mt-8 grid grid-cols-1 gap-6 lg:grid-cols-2" in html
    assert '<dd class="whitespace-nowrap">222.333.444-55</dd>' in html


def test_lista_separa_vencido_de_a_vencer(usuario_logado, alocacao, cliente):
    Cobranca.objects.create(
        cliente=cliente,
        alocacao=alocacao,
        descricao="Franquia",
        valor=Decimal("1200.00"),
        vencimento=HOJE + timedelta(days=2),
    )
    html = usuario_logado.get("/clientes/").content.decode()
    assert "R$ 1.200,00 vencido" not in html
    assert "nada vencido" in html and "R$ 1.200,00" in html


def test_condutor_com_mesmo_nome_nao_duplica(usuario_logado, cliente):
    url = f"/clientes/{cliente.pk}/condutores/novo/"
    usuario_logado.post(url, {"nome": "Maria Silva"})
    resposta = usuario_logado.post(url, {"nome": "maria  SILVA"}, follow=True)
    assert CondutorAutorizado.objects.filter(cliente=cliente).count() == 1
    assert "já é condutor" in resposta.content.decode()
