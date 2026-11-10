"""Etapa 18b — revisão adversarial do financeiro (diff desde a etapa 17b).

- Admin: crédito, caução e as aplicações/movimentações não se apagam pelo
  Admin (apagar sumia com dinheiro sem refazer a conta — a mesma regra que já
  vale para cobrança, ND e recebimento).
- Desfazer movimentação de caução trava o cliente antes da caução (a mesma
  ordem do desconto/depósito): em ordem trocada, duas ações ao mesmo tempo se
  esperariam para sempre no PostgreSQL.
- Caução toda usada para pagar dívidas: "Utilizada", não "Parcialmente utilizada".
"""

import json
import shutil
import subprocess
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.contrib import admin
from django.test import RequestFactory
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.financeiro import services
from apps.financeiro.models import (
    Caucao,
    Cobranca,
    MovimentacaoCaucao,
    MovimentoCredito,
    Recebimento,
)
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente

HOJE = timezone.localdate()


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Rodrigo Revisão", cpf_cnpj="111.222.333-44")


@pytest.fixture
def alocacao(cliente):
    veiculo = Veiculo.objects.create(placa="RTB2C03", marca_modelo="Gol", km_atual=50_000)
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=20),
        valor_semanal=Decimal("650.00"),
        km_entrega=50_000,
        caucao_valor=Decimal("1000.00"),
    )


def _cobranca(cliente, alocacao=None, valor="650.00", vencimento=None):
    return Cobranca.objects.create(
        cliente=cliente,
        alocacao=alocacao,
        origem=Cobranca.Origem.ALUGUEL,
        descricao="Aluguel semanal RTB2C03",
        valor=Decimal(valor),
        vencimento=vencimento or HOJE - timedelta(days=2),
    )


@pytest.fixture
def pedido_admin(django_user_model):
    usuario = django_user_model.objects.create_superuser(
        username="admin", password="senha-forte-123"
    )
    pedido = RequestFactory().get("/admin/")
    pedido.user = usuario
    return pedido


# ---------- Admin: dinheiro não some apagando registro ----------


@pytest.mark.parametrize("modelo", [MovimentoCredito, Caucao])
def test_admin_nao_apaga_credito_nem_caucao(pedido_admin, modelo):
    """Apagar um crédito no Admin dava (ou tirava) dinheiro do cliente sem rastro;
    apagar a caução levava junto os descontos que entraram no DAS."""
    model_admin = admin.site._registry[modelo]
    assert model_admin.has_delete_permission(pedido_admin) is False
    # sem permissão de apagar, a ação "apagar selecionados" some da lista
    assert "delete_selected" not in model_admin.get_actions(pedido_admin)


@pytest.mark.parametrize("modelo_pai", [Recebimento, Caucao])
def test_admin_inlines_de_dinheiro_nao_apagam(pedido_admin, modelo_pai):
    """Apagar a aplicação de um Pix (ou a movimentação da caução) no Admin
    reabria a cobrança e o dinheiro do recebimento sumia do caixa/DAS."""
    inlines = admin.site._registry[modelo_pai].get_inline_instances(pedido_admin)
    assert inlines
    for inline in inlines:
        assert inline.has_delete_permission(pedido_admin) is False


# ---------- desfazer caução: mesma ordem de travas das outras ações ----------


def test_desfazer_movimentacao_caucao_trava_cliente_antes_da_caucao(alocacao):
    caucao = services.abrir_caucao(alocacao, Decimal("1000.00"), data=HOJE, forma="pix")
    movimentacao = caucao.movimentacoes.get()
    ordem = []
    trava_cliente = Cliente.objects.select_for_update
    trava_caucao = services._caucao_travada

    def cliente_travado(*args, **kwargs):
        ordem.append("cliente")
        return trava_cliente(*args, **kwargs)

    def caucao_travada(*args, **kwargs):
        ordem.append("caucao")
        return trava_caucao(*args, **kwargs)

    with (
        mock.patch.object(Cliente.objects, "select_for_update", side_effect=cliente_travado),
        mock.patch.object(services, "_caucao_travada", side_effect=caucao_travada),
    ):
        services.desfazer_movimentacao_caucao(movimentacao)
    assert ordem[:2] == ["cliente", "caucao"]
    assert not MovimentacaoCaucao.objects.exists()


# ---------- status da caução ----------


def test_caucao_toda_usada_em_desconto_e_utilizada(alocacao, cliente):
    caucao = services.abrir_caucao(alocacao, Decimal("650.00"), data=HOJE, forma="pix")
    cobranca = _cobranca(cliente, alocacao)
    services.descontar_da_caucao(caucao, cobranca, Decimal("650.00"), HOJE)
    caucao = Caucao.objects.get(pk=caucao.pk)
    assert caucao.saldo == Decimal("0.00")
    assert caucao.status == "Utilizada"


def test_caucao_parte_usada_continua_parcialmente_utilizada(alocacao, cliente):
    caucao = services.abrir_caucao(alocacao, Decimal("1000.00"), data=HOJE, forma="pix")
    cobranca = _cobranca(cliente, alocacao)
    services.descontar_da_caucao(caucao, cobranca, Decimal("650.00"), HOJE)
    assert Caucao.objects.get(pk=caucao.pk).status == "Parcialmente utilizada"


def test_caucao_usada_e_resto_devolvido_e_devolvida(alocacao, cliente):
    caucao = services.abrir_caucao(alocacao, Decimal("1000.00"), data=HOJE, forma="pix")
    cobranca = _cobranca(cliente, alocacao)
    services.descontar_da_caucao(caucao, cobranca, Decimal("650.00"), HOJE)
    alocacao.encerrar(HOJE, 51_000)
    services.registrar_movimentacao_caucao(
        Caucao.objects.get(pk=caucao.pk), "devolucao", Decimal("350.00"), HOJE
    )
    assert Caucao.objects.get(pk=caucao.pk).status == "Devolvida"


# ---------- conferência dos recebimentos: semana não usada continua fechando ----------


def test_semana_nao_usada_que_vira_credito_nao_acusa_recebimento_incoerente(alocacao, cliente):
    """Ao encerrar, o pagamento de uma semana não usada vira crédito (D03): o
    recebimento continua fechando (aplicado + crédito = valor) — sem alarme falso."""
    semana = _cobranca(cliente, alocacao, vencimento=HOJE)
    services.registrar_recebimento(cliente, HOJE, Decimal("650.00"), "pix", [(semana, "650.00")])
    alocacao.encerrar(HOJE, 51_000)
    assert MovimentoCredito.saldo_do_cliente(cliente) == Decimal("650.00")
    assert services.recebimentos_incoerentes() == []


# ---------- Receber: "1.255" (milhar) não trava o botão ----------


def _funcao_js(html, nome):
    """Corpo de uma função do x-data do Receber, para rodar no Node."""
    inicio = html.index(f"{nome}(texto) {{")
    fim = html.index("\n                },", inicio)
    return "function " + html[inicio:fim] + "\n}"


@pytest.mark.skipif(shutil.which("node") is None, reason="sem Node para rodar o JS da tela")
def test_receber_milhar_com_ponto_nao_acusa_casas_demais(usuario_logado, cliente):
    """O servidor lê "1.255" como R$ 1.255,00 (milhar), mas a tela acusava "mais
    de 2 casas depois da vírgula" e desligava o Confirmar."""
    _cobranca(cliente)
    html = usuario_logado.get(f"/financeiro/receber/?cliente={cliente.pk}").content.decode()
    casos = ["1.255", "12.345", "1.255,00", "650", "650.5", "1.000,0010000", "650,125", "0.125"]
    codigo = (
        _funcao_js(html, "casasDemais")
        + "\n"
        + _funcao_js(html, "br")
        + f"\nconst casos = {json.dumps(casos)};"
        + "\nconsole.log(JSON.stringify([casos.map(casasDemais), casos.map(br)]));"
    )
    saida = subprocess.run(
        ["node", "-e", codigo], capture_output=True, text=True, check=True
    ).stdout
    casas, valores = json.loads(saida)
    # o total da tela lê o milhar como o servidor ("0.125" não é milhar)
    assert valores[:3] == [1255, 12345, 1255]
    assert valores[-1] == 0.125
    assert dict(zip(casos, casas, strict=True)) == {
        "1.255": False,
        "12.345": False,
        "1.255,00": False,
        "650": False,
        "650.5": False,
        "1.000,0010000": True,
        "650,125": True,
        "0.125": True,
    }
    # e o servidor concorda: "1.255" é milhar, "650,125" é recusado
    from apps.campos import dinheiro_br

    assert dinheiro_br("1.255") == Decimal("1255")


# =====================================================================
# Refatoração 18b (financeiro): menos consultas, mesmo resultado
# =====================================================================

from django.db import IntegrityError, connection, transaction  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402

from apps.financeiro.models import AplicacaoRecebimento  # noqa: E402


def _consultas(funcao):
    """(resultado, nº de consultas ao banco) de uma chamada."""
    with CaptureQueriesContext(connection) as ctx:
        resultado = funcao()
    return resultado, len(ctx.captured_queries)


def _aluguel(cliente, alocacao, dias, valor="650.00"):
    return Cobranca.objects.create(
        cliente=cliente,
        alocacao=alocacao,
        origem=Cobranca.Origem.ALUGUEL,
        descricao=f"Aluguel {dias:+d}",
        valor=Decimal(valor),
        vencimento=HOJE + timedelta(days=dias),
    )


# ---------- total quitado: uma fórmula só, igual à das listagens ----------


def test_total_quitado_soma_pagamentos_e_caucao_igual_a_listagem(alocacao, cliente):
    caucao = services.abrir_caucao(alocacao, Decimal("1000.00"), data=HOJE, forma="pix")
    cobranca = _aluguel(cliente, alocacao, -3)
    services.registrar_recebimento(cliente, HOJE, Decimal("200.00"), "pix", [(cobranca, "200")])
    services.descontar_da_caucao(caucao, cobranca, Decimal("150.00"), HOJE)
    cobranca = Cobranca.objects.get(pk=cobranca.pk)
    quitado, consultas = _consultas(lambda: cobranca.total_quitado)
    assert quitado == Decimal("350.00")
    assert cobranca.saldo == Decimal("300.00")
    anotada = Cobranca.com_quitacao_anotada(Cobranca.objects.filter(pk=cobranca.pk)).get()
    assert anotada.total_quitado == quitado
    assert consultas == 1  # antes: 2 (aplicações e caução separadas)


def test_total_quitado_sem_pagamento_e_zero(alocacao, cliente):
    cobranca = _aluguel(cliente, alocacao, -3)
    assert Cobranca.objects.get(pk=cobranca.pk).total_quitado == Decimal("0.00")


# ---------- rotina diária: mesmo resultado, sem 2 consultas por cobrança ----------


@pytest.fixture
def carteira(db):
    """3 clientes com aluguéis em situações variadas, sem o status do dia aplicado."""
    linhas = []
    for i in range(3):
        cli = Cliente.objects.create(nome=f"Motorista {i}", cpf_cnpj=f"222.333.444-{i:02d}")
        carro = Veiculo.objects.create(placa=f"RTB9X{i:02d}", marca_modelo="Gol", km_atual=1)
        aloc = Alocacao.objects.create(
            veiculo=carro,
            cliente=cli,
            data_inicio=HOJE - timedelta(days=60),
            valor_semanal=Decimal("650.00"),
            km_entrega=1,
        )
        cobs = [_aluguel(cli, aloc, d) for d in (-30, -20, -10, -1, 4)]
        services.registrar_recebimento(
            cli, HOJE, Decimal("975.00"), "pix", [(cobs[0], "650"), (cobs[1], "325")]
        )
        linhas.append((cli, cobs))
    cli0, cobs0 = linhas[0]
    Cobranca.objects.filter(pk=cobs0[2].pk).update(status=Cobranca.Status.JUDICIAL)
    cli1, cobs1 = linhas[1]
    # o 2º cliente: só a semana de ontem e a parcial em aberto
    Cobranca.objects.filter(pk__in=[cobs1[2].pk]).update(status=Cobranca.Status.CANCELADA)
    # o 3º: tudo que venceu foi pago ou cancelado; só a semana futura em aberto
    cli2, cobs2 = linhas[2]
    Cobranca.objects.filter(pk__in=[cobs2[1].pk, cobs2[2].pk, cobs2[3].pk]).update(
        status=Cobranca.Status.CANCELADA
    )
    Cliente.objects.filter(pk=cli2.pk).update(status=Cliente.Status.INADIMPLENTE)
    # status "velhos": a rotina precisa acertar
    Cobranca.objects.filter(status__in=[Cobranca.Status.PARCIAL, Cobranca.Status.ATRASADO]).update(
        status=Cobranca.Status.PENDENTE
    )
    return linhas


def test_marcar_atrasos_acerta_status_e_inadimplencia(carteira):
    services.marcar_atrasos(HOJE)
    (c0, cobs0), (c1, cobs1), (c2, cobs2) = carteira
    status = {c.pk: c.status for c in Cobranca.objects.all()}
    S = Cobranca.Status
    assert [status[c.pk] for c in cobs0] == [S.PAGO, S.ATRASADO, S.JUDICIAL, S.ATRASADO, S.PENDENTE]
    assert [status[c.pk] for c in cobs1] == [
        S.PAGO,
        S.ATRASADO,
        S.CANCELADA,
        S.ATRASADO,
        S.PENDENTE,
    ]
    assert [status[c.pk] for c in cobs2] == [
        S.PAGO,
        S.CANCELADA,
        S.CANCELADA,
        S.CANCELADA,
        S.PENDENTE,
    ]
    clientes = dict(Cliente.objects.values_list("pk", "status"))
    assert clientes[c0.pk] == Cliente.Status.INADIMPLENTE
    assert clientes[c1.pk] == Cliente.Status.INADIMPLENTE
    # nada vencido em aberto: o inadimplente volta a ativo
    assert clientes[c2.pk] == Cliente.Status.ATIVO


def test_marcar_atrasos_sem_mudanca_nao_grava_nem_consulta_por_cobranca(carteira):
    services.marcar_atrasos(HOJE)
    versoes = Cobranca.history.count()
    _, consultas = _consultas(lambda: services.marcar_atrasos(HOJE))
    assert Cobranca.history.count() == versoes  # nada mudou: nenhuma versão repetida
    assert consultas <= 6  # antes: 2 por cobrança em aberto


def test_marcar_atrasos_confere_no_banco_antes_de_mudar(carteira):
    """Um pagamento que entra enquanto a rotina roda não é desfeito por ela:
    a mudança de status é conferida com o saldo lido na hora."""
    (cli, cobs), *_ = carteira
    services.marcar_atrasos(HOJE)
    semana = Cobranca.objects.get(pk=cobs[4].pk)  # a vencer, pendente
    anotada = list(Cobranca.com_quitacao_anotada(Cobranca.objects.filter(pk=semana.pk)))
    services.registrar_recebimento(cli, HOJE, Decimal("650.00"), "pix", [(semana, "650")])
    # a rotina "vê" a cobrança como estava antes do pagamento (anotação velha)
    original = Cobranca.com_quitacao_anotada
    chamadas = iter([anotada])

    def velha_na_1a_vez(queryset=None):
        return next(chamadas, None) or original(queryset)

    with mock.patch.object(Cobranca, "com_quitacao_anotada", side_effect=velha_na_1a_vez):
        services.marcar_atrasos(HOJE + timedelta(days=10))
    assert Cobranca.objects.get(pk=semana.pk).status == Cobranca.Status.PAGO


# ---------- Receber: trava as cobranças e relê o saldo numa consulta só ----------


def _ordem_das_travas(funcao):
    """Os modelos travados (select_for_update), na ordem em que foram travados."""
    from django.db.models import QuerySet

    ordem = []
    original = QuerySet.select_for_update

    def espiao(self, *args, **kwargs):
        ordem.append(self.model.__name__)
        return original(self, *args, **kwargs)

    with mock.patch.object(QuerySet, "select_for_update", espiao):
        funcao()
    return ordem


def test_receber_trava_cliente_e_cobrancas_com_poucas_consultas(alocacao, cliente):
    semanas = [_aluguel(cliente, alocacao, d) for d in (-9, -2)]
    ordem = []
    _, consultas = _consultas(
        lambda: ordem.extend(
            _ordem_das_travas(
                lambda: services.registrar_recebimento(
                    cliente, HOJE, Decimal("1300.00"), "pix", [(s, "650") for s in semanas]
                )
            )
        )
    )
    assert ordem == ["Cliente", "Cobranca"]
    assert consultas <= 17  # antes: 26 (o saldo de cada cobrança era somado 2 vezes)
    assert all(c.status == Cobranca.Status.PAGO for c in Cobranca.objects.filter(cliente=cliente))


def test_receber_recusa_cobranca_paga_depois_que_a_tela_abriu(alocacao, cliente):
    semana = _aluguel(cliente, alocacao, -2)
    tela = Cobranca.com_quitacao_anotada(Cobranca.objects.filter(pk=semana.pk)).get()
    services.registrar_recebimento(cliente, HOJE, Decimal("600.00"), "pix", [(semana, "600")])
    with pytest.raises(Exception, match="só faltam R\\$ 50,00"):
        services.registrar_recebimento(
            cliente, HOJE, Decimal("650.00"), "dinheiro", [(tela, "650")]
        )


# ---------- ajuste de valor do aluguel: trava o cliente primeiro ----------


def test_refazer_alugueis_trava_o_cliente_antes_de_comparar_com_o_pago(alocacao, cliente):
    semana = _aluguel(cliente, alocacao, 2)
    Alocacao.objects.filter(pk=alocacao.pk).update(valor_semanal=Decimal("700.00"))
    alocacao.refresh_from_db()
    alteradas = []
    ordem = _ordem_das_travas(
        lambda: alteradas.extend(services.refazer_alugueis_em_aberto(alocacao, HOJE))
    )
    assert ordem[:1] == ["Cliente"]
    assert [c.pk for c in alteradas] == [semana.pk]
    assert Cobranca.objects.get(pk=semana.pk).valor == Decimal("700.00")


def test_reajuste_da_troca_trava_o_cliente_primeiro(alocacao):
    from types import SimpleNamespace

    troca = SimpleNamespace(
        alocacao=alocacao,
        valor_semanal_ajustado=Decimal("500.00"),
        data_retirada=HOJE + timedelta(days=60),
    )
    ordem = _ordem_das_travas(lambda: services.reajustar_aluguel_pela_troca(troca, HOJE))
    assert ordem[:1] == ["Cliente"]


# ---------- desfazer: a cobrança é recalculada uma vez só ----------


def _judicial_paga(cliente, alocacao):
    semana = _aluguel(cliente, alocacao, -15)
    semana.status = Cobranca.Status.JUDICIAL
    semana.save(update_fields=["status"])
    return semana


def test_desfazer_recebimento_de_judicial_grava_uma_versao_so(alocacao, cliente):
    semana = _judicial_paga(cliente, alocacao)
    rec = services.registrar_recebimento(cliente, HOJE, Decimal("650.00"), "pix", [(semana, "650")])
    assert Cobranca.objects.get(pk=semana.pk).status == Cobranca.Status.PAGO
    versoes = semana.history.count()
    services.desfazer_recebimento(Recebimento.objects.get(pk=rec.pk))
    assert Cobranca.objects.get(pk=semana.pk).status == Cobranca.Status.JUDICIAL
    # antes: Pago → Atrasado → Judicial (o "Atrasado" do meio era falso)
    assert semana.history.count() == versoes + 1


def test_desfazer_recebimento_reabre_cobranca_comum(alocacao, cliente):
    semanas = [_aluguel(cliente, alocacao, d) for d in (-9, 3)]
    rec = services.registrar_recebimento(
        cliente, HOJE, Decimal("1000.00"), "pix", [(semanas[0], "650"), (semanas[1], "350")]
    )
    services.desfazer_recebimento(Recebimento.objects.get(pk=rec.pk))
    status = [Cobranca.objects.get(pk=s.pk).status for s in semanas]
    assert status == [Cobranca.Status.ATRASADO, Cobranca.Status.PENDENTE]
    assert not AplicacaoRecebimento.objects.exists()


def test_desfazer_desconto_da_caucao_de_judicial_volta_a_judicial(alocacao, cliente):
    caucao = services.abrir_caucao(alocacao, Decimal("1000.00"), data=HOJE, forma="pix")
    semana = _judicial_paga(cliente, alocacao)
    mov = services.descontar_da_caucao(caucao, semana, Decimal("650.00"), HOJE)
    versoes = semana.history.count()
    services.desfazer_movimentacao_caucao(mov)
    assert Cobranca.objects.get(pk=semana.pk).status == Cobranca.Status.JUDICIAL
    assert semana.history.count() == versoes + 1


def test_apagar_aplicacao_direto_ainda_reabre_a_cobranca(alocacao, cliente):
    """Fora dos serviços (ex.: encerrar contrato, correção pelo shell) o sinal
    continua recalculando a cobrança cujo pagamento foi apagado."""
    semana = _aluguel(cliente, alocacao, -9)
    services.registrar_recebimento(cliente, HOJE, Decimal("650.00"), "pix", [(semana, "650")])
    AplicacaoRecebimento.objects.get(cobranca=semana).delete()
    assert Cobranca.objects.get(pk=semana.pk).status == Cobranca.Status.ATRASADO


def test_sem_recalculo_automatico_volta_a_ligar_mesmo_com_erro():
    from apps.financeiro.signals import _recalculo_manual, sem_recalculo_automatico

    with pytest.raises(RuntimeError), sem_recalculo_automatico():
        assert _recalculo_manual.get() is True
        raise RuntimeError
    assert _recalculo_manual.get() is False


# ---------- travas no banco: dinheiro torto não grava ----------


def _multa_a_cobrar(alocacao):
    from apps.multas.models import Multa

    return Multa.objects.create(
        veiculo=alocacao.veiculo,
        cliente=alocacao.cliente,
        data_infracao=HOJE - timedelta(days=5),
        valor=Decimal("195.23"),
    )


def test_mesma_multa_nao_entra_em_duas_nds(alocacao, cliente):
    from apps.financeiro.models import ItemNotaDebito, NotaDebito

    multa = _multa_a_cobrar(alocacao)
    nds = [NotaDebito.objects.create(cliente=cliente, data_emissao=HOJE, numero=None) for _ in "ab"]
    ItemNotaDebito.objects.create(nota_debito=nds[0], descricao="Multa", valor=1, multa=multa)
    # itens avulsos (sem multa) continuam livres
    for nd in nds:
        ItemNotaDebito.objects.create(nota_debito=nd, descricao="Avaria", valor=1)
    with pytest.raises(IntegrityError), transaction.atomic():
        ItemNotaDebito.objects.create(nota_debito=nds[1], descricao="Multa", valor=1, multa=multa)


def test_nd_cancelada_solta_a_multa_para_nd_nova(usuario_logado, alocacao, cliente):
    from apps.multas.services import gerar_nd_de_multas

    multa = _multa_a_cobrar(alocacao)
    nd = gerar_nd_de_multas(cliente, [multa], HOJE)
    usuario_logado.post(f"/financeiro/cobranca/{nd.cobranca.pk}/cancelar/")
    nova = gerar_nd_de_multas(cliente, [multa], HOJE)
    assert nova.itens.get().multa_id == multa.pk


def _cria(modelo, **campos):
    with transaction.atomic():
        return modelo.objects.create(**campos)


@pytest.mark.parametrize("valor", ["0.00", "-10.00"])
def test_lancamentos_de_dinheiro_exigem_valor_positivo(alocacao, cliente, valor):
    from apps.financeiro.models import ItemNotaDebito, NotaDebito

    valor = Decimal(valor)
    caucao = services.abrir_caucao(alocacao)
    semana = _aluguel(cliente, alocacao, -2)
    rec = services.registrar_recebimento(cliente, HOJE, Decimal("10.00"), "pix", [])
    nd = NotaDebito.objects.create(cliente=cliente, data_emissao=HOJE, numero=None)
    tentativas = [
        (Recebimento, {"cliente": cliente, "data": HOJE, "valor": valor}),
        (AplicacaoRecebimento, {"recebimento": rec, "cobranca": semana, "valor": valor}),
        (
            MovimentoCredito,
            {"cliente": cliente, "tipo": "entrada", "valor": valor, "data": HOJE},
        ),
        (ItemNotaDebito, {"nota_debito": nd, "descricao": "x", "valor": valor}),
        (
            MovimentacaoCaucao,
            {"caucao": caucao, "tipo": "recebimento", "valor": valor, "data": HOJE},
        ),
    ]
    for modelo, campos in tentativas:
        with pytest.raises(IntegrityError):
            _cria(modelo, **campos)


def test_so_o_desconto_da_caucao_quita_cobranca(alocacao, cliente):
    caucao = services.abrir_caucao(alocacao)
    semana = _aluguel(cliente, alocacao, -2)
    base = {"caucao": caucao, "valor": Decimal("10.00"), "data": HOJE}
    with pytest.raises(IntegrityError):
        _cria(MovimentacaoCaucao, tipo="reforco", cobranca=semana, **base)
    with pytest.raises(IntegrityError):
        _cria(MovimentacaoCaucao, tipo="desconto", **base)
    _cria(MovimentacaoCaucao, tipo="reforco", **base)
    _cria(MovimentacaoCaucao, tipo="desconto", cobranca=semana, **base)


# ---------- tirar da judicial: grava o status novo uma vez só ----------


@pytest.mark.parametrize(
    "dias,pago,esperado",
    [(-10, "0", "atrasado"), (5, "0", "pendente"), (5, "100", "parcial")],
)
def test_reabrir_judicial_grava_o_status_certo_numa_versao(
    usuario_logado, alocacao, cliente, dias, pago, esperado
):
    semana = _aluguel(cliente, alocacao, dias)
    if pago != "0":
        services.registrar_recebimento(cliente, HOJE, Decimal(pago), "pix", [(semana, pago)])
    semana.refresh_from_db()
    semana.status = Cobranca.Status.JUDICIAL
    semana.save(update_fields=["status"])
    versoes = semana.history.count()
    resposta = usuario_logado.post(f"/financeiro/cobranca/{semana.pk}/reabrir/")
    assert resposta.status_code == 302
    assert Cobranca.objects.get(pk=semana.pk).status == esperado
    # antes: Judicial → Pendente → Atrasado (o "Pendente" do meio era falso)
    assert semana.history.count() == versoes + 1


# =====================================================================
# Etapa 18b — achados dos testes no navegador (financeiro)
# =====================================================================

import re  # noqa: E402
from pathlib import Path  # noqa: E402

from django.contrib.messages import get_messages  # noqa: E402

PASTA_TEMPLATES = Path(__file__).resolve().parents[2] / "templates" / "financeiro"


def _mensagens(resposta):
    return [str(m) for m in get_messages(resposta.wsgi_request)]


def _xdata_do_receber(html):
    """O objeto do x-data do formulário do Receber, como texto JS."""
    inicio = html.index("x-data='{ valor: 0") + len("x-data='")
    fim = html.index("}'\n      x-init=", inicio) + 1
    return html[inicio:fim]


@pytest.mark.skipif(shutil.which("node") is None, reason="sem Node para rodar o JS da tela")
def test_receber_botao_confirmar_nao_fica_travado_sem_aviso(usuario_logado, cliente):
    """O Alpine só tira o "disabled" com false/null: a expressão devolvia "" (texto
    vazio) e o Confirmar ficava travado para sempre, com cara de ativo."""
    semana = _cobranca(cliente)
    html = usuario_logado.get(f"/financeiro/receber/?cliente={cliente.pk}").content.decode()
    assert 'x-bind:disabled="bloqueado"' in html
    codigo = (
        "const campo = { value: '', disabled: false,"
        " dataset: { saldo: '650.00', descricao: 'Aluguel' } };"
        "globalThis.document = { querySelectorAll: () => [campo],"
        " getElementById: () => ({ value: campo.value }) };"
        f"const d = ({_xdata_do_receber(html)});"
        "const casos = [];"
        "const pares = [['650,00', '650,00'], ['700,00', '650,00'], ['700,00', '700,00']];"
        "for (const [valor, aplicar] of pares) {"
        "  campo.value = aplicar; d.valor = d.br(valor); d.recalc(); casos.push(d.bloqueado); }"
        "console.log(JSON.stringify(casos));"
    )
    saida = subprocess.run(["node", "-e", codigo], capture_output=True, text=True, check=True)
    # pagamento certo e com sobra: liberado (false de verdade, não ""); acima da cobrança: travado
    assert json.loads(saida.stdout) == [False, False, True]
    assert semana.pk  # a linha da cobrança estava na tela


def test_templates_do_financeiro_nao_usam_texto_no_disabled():
    """Propriedade iniciada com texto ("") dentro de um x-bind:disabled trava o botão."""
    for caminho in PASTA_TEMPLATES.glob("*.html"):
        html = caminho.read_text()
        textos = set(re.findall(r'(\w+): ""', html))
        for expressao in re.findall(r'x-bind:disabled="([^"]*)"', html):
            nomes = set(re.findall(r"(?<![.\w!])(\w+)(?![\w(])", expressao.replace("!!", "")))
            assert not (nomes & textos), f"{caminho.name}: {expressao}"


def test_receber_reenvio_do_mesmo_formulario_nao_lanca_em_dobro(usuario_logado, cliente):
    """ "Voltar" depois do recibo e Confirmar de novo, mesmo passados os 2 minutos."""
    _cobranca(cliente)
    html = usuario_logado.get(f"/financeiro/receber/?cliente={cliente.pk}").content.decode()
    token = re.search(r'name="token" value="(\w+)"', html).group(1)
    dados = {
        "cliente": cliente.pk,
        "token": token,
        "valor": "21,00",
        "data": HOJE.isoformat(),
        "forma": "pix",
        "sobra_destino": "credito",
    }
    primeira = usuario_logado.post("/financeiro/receber/", dados)
    original = Recebimento.objects.get()
    assert f"recibo={original.pk}" in primeira["Location"]
    # a trava de 2 minutos já não pega: só o token barra
    with mock.patch.object(services, "JANELA_RECEBIMENTO_REPETIDO", timedelta(seconds=-1)):
        segunda = usuario_logado.post("/financeiro/receber/", dados)
    assert Recebimento.objects.count() == 1
    assert f"recibo={original.pk}" in segunda["Location"]
    assert any("já tinha sido lançado" in m for m in _mensagens(segunda))
    assert MovimentoCredito.saldo_do_cliente(cliente) == Decimal("21.00")


def test_alocacao_com_inicio_no_passado_ja_nasce_atrasada(cliente):
    veiculo = Veiculo.objects.create(placa="RTB7P01", marca_modelo="Gol", km_atual=1)
    alocacao = Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=28),
        valor_semanal=Decimal("700.00"),
        km_entrega=1,
    )
    criadas = services.gerar_cobrancas_semanais(alocacao=alocacao)
    assert len(criadas) >= 4
    for cobranca in Cobranca.objects.filter(alocacao=alocacao):
        esperado = (
            Cobranca.Status.ATRASADO if cobranca.vencimento < HOJE else Cobranca.Status.PENDENTE
        )
        assert cobranca.status == esperado
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.INADIMPLENTE


def test_caucao_usada_com_carro_com_o_cliente_pede_para_repor(usuario_logado, alocacao, cliente):
    caucao = Caucao.objects.get(alocacao=alocacao)
    services.registrar_movimentacao_caucao(caucao, "recebimento", Decimal("1000"), HOJE, "pix")
    services.registrar_movimentacao_caucao(
        caucao, "reforco", Decimal("100"), HOJE, "pix", acima_do_combinado_confirmado=True
    )
    semana = _cobranca(cliente, alocacao, valor="700.00")
    services.descontar_da_caucao(caucao, semana, Decimal("700.00"), HOJE)
    html = usuario_logado.get(f"/financeiro/caucoes/{caucao.pk}/").content.decode()
    assert "completa ✓" not in html
    assert "falta repor R$ 600,00" in html
    # o formulário já vem em "Cliente reforçou a caução" com o que falta repor
    assert 'tipo: "reforco"' in html
    assert 'value="600,00"' in html


def test_caucao_completa_sem_desconto_continua_completa(usuario_logado, alocacao):
    caucao = Caucao.objects.get(alocacao=alocacao)
    services.registrar_movimentacao_caucao(caucao, "recebimento", Decimal("1000"), HOJE, "pix")
    html = usuario_logado.get(f"/financeiro/caucoes/{caucao.pk}/").content.decode()
    assert "completa ✓" in html and "falta repor" not in html


def test_nd_recusa_emissao_no_futuro(cliente):
    with pytest.raises(Exception, match="não pode ser no futuro"):
        services.emitir_nota_debito(
            cliente, HOJE + timedelta(days=365), [("Lavagem", Decimal("80"))]
        )


def test_nd_vencimento_distante_pede_confirmacao(usuario_logado, cliente):
    dados = {
        "cliente": cliente.pk,
        "data_emissao": HOJE.isoformat(),
        "vencimento": (HOJE + timedelta(days=365)).isoformat(),
        "item_descricao": "Lavagem",
        "item_valor": "80,00",
        "token": "t-distante-1",
    }
    resposta = usuario_logado.post("/financeiro/nd/nova/", dados)
    assert resposta.status_code == 200
    assert "Confira o ano do vencimento" in resposta.content.decode()
    assert not Cobranca.objects.filter(cliente=cliente).exists()
    dados.update(token="t-distante-2", confirmar_vencimento_distante="1")
    assert usuario_logado.post("/financeiro/nd/nova/", dados).status_code == 302
    assert Cobranca.objects.filter(cliente=cliente).count() == 1


def test_nd_nova_tem_data_maxima_e_campos_empilhados_no_celular(usuario_logado, db):
    html = usuario_logado.get("/financeiro/nd/nova/").content.decode()
    assert f'max="{HOJE:%Y-%m-%d}"' in html
    assert "basis-full sm:basis-auto sm:flex-1" in html
    assert 'x-bind:disabled="bloqueado"' in html


def test_extrato_mostra_a_caucao_e_liga_para_ela(usuario_logado, alocacao, cliente):
    caucao = Caucao.objects.get(alocacao=alocacao)
    services.registrar_movimentacao_caucao(caucao, "recebimento", Decimal("1000"), HOJE, "pix")
    nd = _cobranca(cliente, alocacao, valor="430.50")
    services.descontar_da_caucao(caucao, nd, Decimal("430.50"), HOJE)
    html = usuario_logado.get(f"/financeiro/cliente/{cliente.pk}/").content.decode()
    assert "data-caucao-extrato" in html
    assert "Desconto na caução (não é dinheiro novo)" in html
    assert "pagou: Aluguel semanal RTB2C03" in html
    assert f"/financeiro/caucoes/{caucao.pk}/" in html
    assert "data-link-caucao" in html
    assert "data-lista-cortada" not in html


def test_extrato_avisa_quando_corta_a_lista(usuario_logado, cliente, monkeypatch):
    from apps.financeiro import views

    monkeypatch.setattr(views, "LIMITE_COBRANCAS_EXTRATO", 2)
    for _ in range(3):
        _cobranca(cliente)
    html = usuario_logado.get(f"/financeiro/cliente/{cliente.pk}/").content.decode()
    assert "Mostrando as 2 mais recentes." in html


def test_sobra_para_caucao_acima_do_combinado_pede_confirmacao(usuario_logado, alocacao, cliente):
    caucao = Caucao.objects.get(alocacao=alocacao)
    services.registrar_movimentacao_caucao(caucao, "recebimento", Decimal("1000"), HOJE, "pix")
    semana = _cobranca(cliente, alocacao, valor="700.00")
    dados = {
        "cliente": cliente.pk,
        "valor": "800,00",
        "data": HOJE.isoformat(),
        "forma": "pix",
        f"aplicar_{semana.pk}": "700,00",
        "sobra_destino": "caucao",
        "token": "sobra-1",
    }
    resposta = usuario_logado.post("/financeiro/receber/", dados)
    assert resposta.status_code == 200
    assert "Confirmo: caução acima do combinado" in resposta.content.decode()
    assert not Recebimento.objects.exists()
    assert caucao.saldo == Decimal("1000.00")
    dados.update(token="sobra-2", confirmar_caucao_acima="1")
    assert usuario_logado.post("/financeiro/receber/", dados).status_code == 302
    assert caucao.saldo == Decimal("1100.00")


def test_credito_junto_usa_so_o_que_o_dinheiro_nao_cobre(cliente):
    semana = _cobranca(cliente, valor="650.00")
    MovimentoCredito.objects.create(
        cliente=cliente, tipo=MovimentoCredito.Tipo.ENTRADA, valor=Decimal("150.00"), data=HOJE
    )
    recebimento = services.registrar_recebimento(
        cliente, HOJE, Decimal("600.00"), "pix", [(semana, "650")], usar_credito=Decimal("150")
    )
    assert recebimento.recebimento_credito.valor == Decimal("50.00")
    assert MovimentoCredito.saldo_do_cliente(cliente) == Decimal("100.00")
    # nenhuma parte do Pix virou crédito "de mentira"
    assert not recebimento.movimentos_credito.filter(tipo=MovimentoCredito.Tipo.ENTRADA).exists()
    semana.refresh_from_db()
    assert semana.status == Cobranca.Status.PAGO


def test_credito_junto_sem_precisar_do_credito_nao_usa_nada(cliente):
    semana = _cobranca(cliente, valor="650.00")
    MovimentoCredito.objects.create(
        cliente=cliente, tipo=MovimentoCredito.Tipo.ENTRADA, valor=Decimal("850.00"), data=HOJE
    )
    recebimento = services.registrar_recebimento(
        cliente, HOJE, Decimal("700.00"), "pix", [(semana, "650")], usar_credito=Decimal("850")
    )
    assert recebimento.recebimento_credito is None
    assert MovimentoCredito.saldo_do_cliente(cliente) == Decimal("900.00")  # 850 + sobra 50


def test_lista_de_caucoes_em_ordem_e_acertadas_escondidas(usuario_logado, cliente):
    def caucao_de(nome, placa, ativa, deposito):
        cli = Cliente.objects.create(nome=nome, cpf_cnpj=f"{placa}-cpf")
        carro = Veiculo.objects.create(placa=placa, marca_modelo="Gol", km_atual=1)
        aloc = Alocacao.objects.create(
            veiculo=carro,
            cliente=cli,
            data_inicio=HOJE - timedelta(days=30),
            valor_semanal=Decimal("650"),
            km_entrega=1,
            caucao_valor=Decimal("1000"),
        )
        caucao = Caucao.objects.get(alocacao=aloc)
        if deposito:
            services.registrar_movimentacao_caucao(caucao, "recebimento", deposito, HOJE, "pix")
        if not ativa:
            Alocacao.objects.filter(pk=aloc.pk).update(
                status=Alocacao.Status.ENCERRADA, data_termino=HOJE
            )
        return caucao

    caucao_de("Zeca Ativo", "RTB1Z01", True, Decimal("1000"))
    caucao_de("Ana Ativa", "RTB1A01", True, None)
    caucao_de("Bia Pendente", "RTB1B01", False, Decimal("500"))
    caucao_de("Caio Acertado", "RTB1C01", False, None)
    html = usuario_logado.get("/financeiro/caucoes/").content.decode()
    assert html.index("Ana Ativa") < html.index("Zeca Ativo") < html.index("Bia Pendente")
    assert "acerto pendente</span>" in html
    assert "Caio Acertado" not in html
    assert "Mostrar as já acertadas (1)" in html
    todas = usuario_logado.get("/financeiro/caucoes/?acertadas=1").content.decode()
    assert todas.index("Bia Pendente") < todas.index("Caio Acertado")


def test_extrato_esconde_encargo_de_lancamento_desfeito(usuario_logado, cliente):
    semana = _cobranca(cliente, vencimento=HOJE - timedelta(days=10))
    semana.atualizar_status()
    dados = {
        "cliente": cliente.pk,
        "valor": "715,00",
        "data": HOJE.isoformat(),
        "forma": "pix",
        f"aplicar_{semana.pk}": "650,00",
        f"incluir_encargo_{semana.pk}": "1",
        f"encargo_{semana.pk}": "65,00",
        f"aplicar_encargo_{semana.pk}": "65,00",
        "token": "encargo-1",
    }
    assert usuario_logado.post("/financeiro/receber/", dados).status_code == 302
    recebimento = Recebimento.objects.get()
    usuario_logado.post(f"/financeiro/recebimento/{recebimento.pk}/desfazer/")
    encargo = semana.encargos.get()
    assert encargo.status == Cobranca.Status.CANCELADA  # o registro continua (auditoria)
    html = usuario_logado.get(f"/financeiro/cliente/{cliente.pk}/").content.decode()
    assert encargo.descricao not in html
    # encargo cancelado por outro motivo continua aparecendo
    Cobranca.objects.filter(pk=encargo.pk).update(observacoes="cancelado à mão")
    html = usuario_logado.get(f"/financeiro/cliente/{cliente.pk}/").content.decode()
    assert encargo.descricao in html


def _encerrar(alocacao):
    Alocacao.objects.filter(pk=alocacao.pk).update(
        status=Alocacao.Status.ENCERRADA, data_termino=HOJE
    )


def test_acerto_da_caucao_com_credito_que_cobre_nao_abre_no_desconto(
    usuario_logado, alocacao, cliente
):
    caucao = Caucao.objects.get(alocacao=alocacao)
    services.registrar_movimentacao_caucao(caucao, "recebimento", Decimal("1000"), HOJE, "pix")
    _cobranca(cliente, alocacao, valor="350.00")
    MovimentoCredito.objects.create(
        cliente=cliente, tipo=MovimentoCredito.Tipo.ENTRADA, valor=Decimal("700.00"), data=HOJE
    )
    _encerrar(alocacao)
    html = usuario_logado.get(f"/financeiro/caucoes/{caucao.pk}/").content.decode()
    assert 'tipo: "devolucao"' in html
    assert "data-usar-credito" in html
    assert 'value="350,00"' not in html


def test_acerto_da_caucao_com_credito_parcial_desconta_so_o_resto(
    usuario_logado, alocacao, cliente
):
    caucao = Caucao.objects.get(alocacao=alocacao)
    services.registrar_movimentacao_caucao(caucao, "recebimento", Decimal("1000"), HOJE, "pix")
    primeira = _cobranca(cliente, alocacao, valor="350.00", vencimento=HOJE - timedelta(days=5))
    segunda = _cobranca(cliente, alocacao, valor="200.00", vencimento=HOJE - timedelta(days=2))
    MovimentoCredito.objects.create(
        cliente=cliente, tipo=MovimentoCredito.Tipo.ENTRADA, valor=Decimal("400.00"), data=HOJE
    )
    _encerrar(alocacao)
    html = usuario_logado.get(f"/financeiro/caucoes/{caucao.pk}/").content.decode()
    assert 'tipo: "desconto"' in html
    # o crédito paga a 1ª (350) e 50 da 2ª: a caução só entra nos 150 que sobram
    assert 'value="150,00"' in html
    assert re.search(rf'value="{segunda.pk}"[^>]*selected', html)
    assert not re.search(rf'value="{primeira.pk}"[^>]*selected', html)


def test_das_com_mes_invalido_avisa(usuario_logado, db):
    resposta = usuario_logado.get("/financeiro/das/?mes=2026-13")
    assert resposta.status_code == 200
    assert any(m.startswith("Mês inválido — mostrando") for m in _mensagens(resposta))
    certo = usuario_logado.get(f"/financeiro/das/?mes={HOJE:%Y-%m}")
    assert not any("inválido" in m for m in _mensagens(certo))


def test_mais_das_cobrancas_tem_44px_no_celular():
    html = (PASTA_TEMPLATES / "cobrancas.html").read_text()
    resumo = re.search(r"<summary[^>]*>Mais ▾</summary>", html).group(0)
    assert "min-h-11" in resumo


# =====================================================================
# Etapa 18b — revisão adversarial das correções do navegador (financeiro)
# =====================================================================


@pytest.mark.skipif(shutil.which("node") is None, reason="sem Node para rodar o JS da tela")
def test_receber_sobra_para_caucao_acima_trava_ate_confirmar(usuario_logado, alocacao, cliente):
    """A caixa "Confirmo: caução acima do combinado" trava o botão até ser marcada —
    e só quando a sobra vai para a caução e passa do combinado."""
    _cobranca(cliente, alocacao)
    html = usuario_logado.get(f"/financeiro/receber/?cliente={cliente.pk}").content.decode()
    codigo = (
        "const campo = { value: '', disabled: false,"
        " dataset: { saldo: '650.00', descricao: 'Aluguel' } };"
        "globalThis.document = { querySelectorAll: () => [campo],"
        " getElementById: () => ({ value: campo.value }) };"
        f"const d = ({_xdata_do_receber(html)});"
        "d.caucaoSaldo = 1000; d.caucaoCombinado = 1000; campo.value = '650,00';"
        "const casos = [];"
        "for (const [valor, destino, confirma] of ["
        "  ['700,00', 'credito', false], ['700,00', 'caucao', false],"
        "  ['700,00', 'caucao', true], ['650,00', 'caucao', false]]) {"
        "  d.valor = d.br(valor); d.destino = destino; d.confirmaCaucaoAcima = confirma;"
        "  d.recalc(); casos.push([d.sobra, d.caucaoAcima, d.bloqueado]); }"
        "console.log(JSON.stringify(casos));"
    )
    saida = subprocess.run(["node", "-e", codigo], capture_output=True, text=True, check=True)
    assert json.loads(saida.stdout) == [
        [50, False, False],  # sobra vira crédito: nada a confirmar
        [50, True, True],  # caução ficaria em 1.050 (> 1.000): trava
        [50, True, False],  # confirmado: libera
        [0, False, False],  # sem sobra: nada a confirmar
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="sem Node para rodar o JS da tela")
def test_receber_sobra_com_credito_junto_conta_so_o_dinheiro_novo(usuario_logado, cliente):
    """Pix de 600 + crédito numa semana de 650: não sobra nada (o crédito só cobre os 50)."""
    _cobranca(cliente)
    html = usuario_logado.get(f"/financeiro/receber/?cliente={cliente.pk}").content.decode()
    codigo = (
        "const campo = { value: '650,00', disabled: false,"
        " dataset: { saldo: '650.00', descricao: 'Aluguel' } };"
        "globalThis.document = { querySelectorAll: () => [campo],"
        " getElementById: () => ({ value: campo.value }) };"
        f"const d = ({_xdata_do_receber(html)});"
        "d.credito = 150; d.usarJunto = true; d.valor = 600; d.recalc();"
        "const a = [d.sobra, d.bloqueado];"
        "d.valor = 700; d.recalc();"
        "console.log(JSON.stringify([a, [d.sobra, d.bloqueado]]));"
    )
    saida = subprocess.run(["node", "-e", codigo], capture_output=True, text=True, check=True)
    assert json.loads(saida.stdout) == [[0, False], [50, False]]


def test_receber_erro_nao_gasta_o_token(usuario_logado, cliente):
    """Envio recusado (valor errado) não queima o token: corrigido, o mesmo formulário lança."""
    _cobranca(cliente)
    dados = {
        "cliente": cliente.pk,
        "token": "token-com-erro",
        "valor": "abc",
        "data": HOJE.isoformat(),
        "forma": "pix",
        "sobra_destino": "credito",
    }
    assert usuario_logado.post("/financeiro/receber/", dados).status_code == 200
    assert not Recebimento.objects.exists()
    dados["valor"] = "30,00"
    resposta = usuario_logado.post("/financeiro/receber/", dados)
    assert resposta.status_code == 302
    assert Recebimento.objects.count() == 1


def test_receber_token_repetido_de_outro_cliente_nao_mostra_recibo_alheio(usuario_logado, cliente):
    """O recibo lembrado pelo token só é mostrado se for do mesmo cliente."""
    _cobranca(cliente)
    outro = Cliente.objects.create(nome="Outro Cliente", cpf_cnpj="999.888.777-66")
    dados = {
        "cliente": cliente.pk,
        "token": "token-unico",
        "valor": "30,00",
        "data": HOJE.isoformat(),
        "forma": "pix",
        "sobra_destino": "credito",
    }
    assert usuario_logado.post("/financeiro/receber/", dados).status_code == 302
    dados["cliente"] = outro.pk
    resposta = usuario_logado.post("/financeiro/receber/", dados)
    assert resposta.status_code == 302
    assert "recibo=" not in resposta["Location"]
    assert Recebimento.objects.count() == 1


def test_caucao_deposito_incompleto_e_usada_mostra_as_duas_faltas(
    usuario_logado, alocacao, cliente
):
    """Depositou 500 de 1.000 e 200 pagaram uma cobrança: faltam 500 + 200 para o combinado."""
    caucao = Caucao.objects.get(alocacao=alocacao)
    services.registrar_movimentacao_caucao(caucao, "recebimento", Decimal("500"), HOJE, "pix")
    semana = _cobranca(cliente, alocacao, valor="200.00")
    services.descontar_da_caucao(caucao, semana, Decimal("200.00"), HOJE)
    html = usuario_logado.get(f"/financeiro/caucoes/{caucao.pk}/").content.decode()
    assert "falta R$ 500,00" in html
    assert "falta repor R$ 200,00" in html
    assert 'tipo: "recebimento"' in html
    # o valor sugerido leva a caução de volta ao combinado (sem cair na trava de "acima")
    assert 'value="700,00"' in html
    dados = {
        "tipo": "recebimento",
        "valor": "700,00",
        "data": HOJE.isoformat(),
        "forma": "pix",
        "token": "deposito-completo",
    }
    resposta = usuario_logado.post(f"/financeiro/caucoes/{caucao.pk}/", dados)
    assert resposta.status_code == 302
    assert Caucao.com_saldos_anotados().get(pk=caucao.pk).saldo == Decimal("1000.00")
    html = usuario_logado.get(f"/financeiro/caucoes/{caucao.pk}/").content.decode()
    assert "falta repor" not in html and "completa ✓" in html


def test_caucao_encerrada_sem_divida_abre_na_devolucao_do_saldo(usuario_logado, alocacao):
    caucao = Caucao.objects.get(alocacao=alocacao)
    services.registrar_movimentacao_caucao(caucao, "recebimento", Decimal("1000"), HOJE, "pix")
    _encerrar(alocacao)
    html = usuario_logado.get(f"/financeiro/caucoes/{caucao.pk}/").content.decode()
    assert 'tipo: "devolucao"' in html
    assert 'value="1.000,00"' in html
    assert "falta repor" not in html


def _caucoes_com_cliente(qtd, prefixo):
    for i in range(qtd):
        cli = Cliente.objects.create(nome=f"{prefixo} {i}", cpf_cnpj=f"{prefixo}-{i}")
        carro = Veiculo.objects.create(placa=f"{prefixo[:3]}{i}A{i:02d}", marca_modelo="Gol")
        aloc = Alocacao.objects.create(
            veiculo=carro,
            cliente=cli,
            data_inicio=HOJE - timedelta(days=30),
            valor_semanal=Decimal("650"),
            km_entrega=1,
            caucao_valor=Decimal("1000"),
        )
        caucao = Caucao.objects.get(alocacao=aloc)
        services.registrar_movimentacao_caucao(caucao, "recebimento", Decimal("400"), HOJE, "pix")
        if i % 2:
            Alocacao.objects.filter(pk=aloc.pk).update(
                status=Alocacao.Status.ENCERRADA, data_termino=HOJE
            )


def test_lista_de_caucoes_sem_consulta_por_linha(usuario_logado, db):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    _caucoes_com_cliente(2, "Poucas")
    usuario_logado.get("/financeiro/caucoes/")  # 1ª visita grava a sessão
    with CaptureQueriesContext(connection) as poucas:
        assert usuario_logado.get("/financeiro/caucoes/?acertadas=1").status_code == 200
    _caucoes_com_cliente(6, "Muitas")
    with CaptureQueriesContext(connection) as muitas:
        assert usuario_logado.get("/financeiro/caucoes/?acertadas=1").status_code == 200
    assert len(muitas) == len(poucas)


def test_extrato_sem_consulta_por_movimentacao_da_caucao(usuario_logado, alocacao, cliente):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    caucao = Caucao.objects.get(alocacao=alocacao)
    services.registrar_movimentacao_caucao(caucao, "recebimento", Decimal("1000"), HOJE, "pix")
    usuario_logado.get(f"/financeiro/cliente/{cliente.pk}/")  # 1ª visita grava a sessão
    with CaptureQueriesContext(connection) as poucas:
        usuario_logado.get(f"/financeiro/cliente/{cliente.pk}/")
    for dias, valor in ((2, "100.00"), (3, "120.00"), (4, "130.00")):
        semana = _cobranca(cliente, alocacao, valor=valor, vencimento=HOJE - timedelta(days=dias))
        services.descontar_da_caucao(caucao, semana, Decimal(valor), HOJE)
    with CaptureQueriesContext(connection) as muitas:
        html = usuario_logado.get(f"/financeiro/cliente/{cliente.pk}/").content.decode()
    assert html.count("Desconto na caução (não é dinheiro novo)") == 3
    assert len(muitas) == len(poucas)


def test_nd_vencimento_em_60_dias_nao_pede_confirmacao(cliente):
    nd = services.emitir_nota_debito(
        cliente, HOJE, [("Lavagem", Decimal("80"))], vencimento=HOJE + timedelta(days=60)
    )
    assert nd.cobranca.vencimento == HOJE + timedelta(days=60)
    with pytest.raises(Exception, match="Confirmo: vencimento distante"):
        services.emitir_nota_debito(
            cliente, HOJE, [("Lavagem", Decimal("80"))], vencimento=HOJE + timedelta(days=61)
        )


def test_nd_emissao_de_hoje_e_do_passado_continuam_valendo(cliente):
    services.emitir_nota_debito(cliente, HOJE, [("Lavagem", Decimal("80"))])
    services.emitir_nota_debito(cliente, HOJE - timedelta(days=30), [("Multa", Decimal("90"))])
    assert Cobranca.objects.filter(cliente=cliente).count() == 2


def test_alocacao_que_comeca_hoje_nao_deixa_cliente_inadimplente(cliente):
    veiculo = Veiculo.objects.create(placa="RTB7H01", marca_modelo="Gol", km_atual=1)
    alocacao = Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE,
        valor_semanal=Decimal("700.00"),
        km_entrega=1,
    )
    services.gerar_cobrancas_semanais(alocacao=alocacao)
    assert not Cobranca.objects.filter(alocacao=alocacao, status=Cobranca.Status.ATRASADO).exists()
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.ATIVO


def test_das_com_mes_fora_do_intervalo_avisa(usuario_logado, db):
    resposta = usuario_logado.get("/financeiro/das/?mes=1999-05")
    assert resposta.status_code == 200
    assert any(m.startswith("Mês inválido — mostrando") for m in _mensagens(resposta))


def test_das_exportar_com_mes_invalido_volta_para_a_tela(usuario_logado, db):
    """A planilha de um mês que não foi o pedido não baixa calada: volta à tela com o aviso."""
    resposta = usuario_logado.get("/financeiro/das/?mes=2026-13&exportar=csv")
    assert resposta.status_code == 302
    assert "exportar" not in resposta["Location"]
    tela = usuario_logado.get(resposta["Location"])
    assert any(m.startswith("Mês inválido — mostrando") for m in _mensagens(tela))
    certo = usuario_logado.get(f"/financeiro/das/?mes={HOJE:%Y-%m}&exportar=csv")
    assert certo.status_code == 200
