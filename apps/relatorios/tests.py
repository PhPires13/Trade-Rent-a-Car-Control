from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.alocacoes.models import Alocacao
from apps.financeiro import services as financeiro
from apps.financeiro.models import Cobranca
from apps.frota.alertas import vigencias_a_vencer
from apps.frota.models import Veiculo
from apps.manutencao.models import Manutencao
from apps.pessoas.models import Cliente
from apps.relatorios import services
from apps.sinistros.models import AuxilioMotorista, Sinistro


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(
        placa="QXQ6C10",
        marca_modelo="Gol",
        mensalidade_protecao=Decimal("304.00"),
        data_aquisicao=date(2025, 1, 1),
    )


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


def test_despesas_do_mes(veiculo, db):
    Manutencao.objects.create(
        veiculo=veiculo,
        tipo="corretiva",
        data=date(2026, 7, 10),
        descricao="Freios",
        custo_real=Decimal("400.00"),
    )
    Manutencao.objects.create(  # fora do mês — não entra
        veiculo=veiculo,
        tipo="corretiva",
        data=date(2026, 6, 10),
        descricao="Óleo",
        custo_real=Decimal("150.00"),
    )
    Sinistro.objects.create(
        veiculo=veiculo,
        data=date(2026, 7, 5),
        envolvido="terceiro",
        acionou_protecao=True,
        data_evento=date(2026, 7, 6),
        franquia_valor=Decimal("1500.00"),
    )
    despesas = services.despesas_do_mes(2026, 7)
    assert despesas["total_manutencao"] == Decimal("400.00")
    assert despesas["total_franquias"] == Decimal("1500.00")
    assert despesas["total_protecao"] == Decimal("304.00")
    assert despesas["total_geral"] == Decimal("2204.00")


def test_receitas_do_mes_inclui_outros_creditos(veiculo, cliente):
    sinistro = Sinistro.objects.create(veiculo=veiculo, data=date(2026, 7, 1), envolvido="terceiro")
    AuxilioMotorista.objects.create(
        sinistro=sinistro,
        valor=Decimal("1518.00"),
        status="recebido",
        data_recebimento=date(2026, 7, 15),
    )
    receitas = services.receitas_do_mes(2026, 7)
    assert receitas["total_auxilios"] == Decimal("1518.00")


def test_recebiveis_em_aberto_agrupa_por_cliente(veiculo, cliente):
    Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
    )
    financeiro.gerar_cobrancas_semanais(hoje=date(2026, 7, 9))
    recebiveis = services.recebiveis_em_aberto()
    assert len(recebiveis) == 1
    assert recebiveis[0]["cliente"] == cliente
    assert recebiveis[0]["total"] == Decimal("1300.00")


def test_cobranca_judicial_aparece_nos_recebiveis(veiculo, cliente):
    Cobranca.objects.create(
        cliente=cliente,
        origem="aluguel",
        descricao="Dívida antiga",
        valor=Decimal("2000.00"),
        vencimento=date(2026, 1, 1),
        status=Cobranca.Status.JUDICIAL,
    )
    recebiveis = services.recebiveis_em_aberto()
    assert recebiveis[0]["judicial"] == Decimal("2000.00")


def test_vigencias_a_vencer(veiculo, cliente, db):
    hoje = date(2026, 7, 20)
    veiculo.rastreador_vigencia_fim = hoje + timedelta(days=10)
    veiculo.bateria_garantia_fim = hoje - timedelta(days=5)  # vencida
    veiculo.save()
    cliente.cnh_validade = hoje + timedelta(days=20)
    cliente.save()
    Veiculo.objects.create(  # vendido não alerta
        placa="RNB9J66",
        marca_modelo="Voyage",
        status="vendido",
        rastreador_vigencia_fim=hoje + timedelta(days=5),
    )
    alertas = vigencias_a_vencer(hoje)
    descricoes = [a["descricao"] for a in alertas]
    assert len(alertas) == 3
    assert alertas[0]["vencido"] is True  # bateria vencida vem primeiro (data menor)
    assert any("CNH de Arlen" in d for d in descricoes)
    assert not any("RNB9J66" in d for d in descricoes)


@pytest.fixture
def usuario_logado(client, django_user_model):
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    client.login(username="dono", password="senha-forte-123")
    return client


def test_tela_de_relatorios_renderiza(usuario_logado, veiculo, cliente):
    resposta = usuario_logado.get("/relatorios/?mes=2026-07")
    assert resposta.status_code == 200
    assert "Receitas do mês" in resposta.content.decode()


def test_exportacoes_xlsx_e_csv(usuario_logado, veiculo, db):
    Manutencao.objects.create(
        veiculo=veiculo,
        tipo="corretiva",
        data=date(2026, 7, 10),
        descricao="Freios",
        custo_real=Decimal("400.00"),
    )
    xlsx = usuario_logado.get("/relatorios/?mes=2026-07&exportar=despesas&formato=xlsx")
    assert xlsx.status_code == 200
    assert "spreadsheetml" in xlsx["Content-Type"]
    assert "despesas-2026-07.xlsx" in xlsx["Content-Disposition"]
    for tipo in ["receitas", "recebiveis", "frota"]:
        csv_resp = usuario_logado.get(f"/relatorios/?mes=2026-07&exportar={tipo}&formato=csv")
        assert csv_resp.status_code == 200
        assert csv_resp["Content-Type"] == "text/csv"


def test_painel_consolidado_renderiza(usuario_logado, veiculo, cliente):
    veiculo.rastreador_vigencia_fim = date.today() + timedelta(days=10)
    veiculo.save()
    resposta = usuario_logado.get("/")
    assert resposta.status_code == 200
    conteudo = resposta.content.decode()
    assert "Vigências e documentos a vencer" in conteudo
    assert "ocupação" in conteudo
