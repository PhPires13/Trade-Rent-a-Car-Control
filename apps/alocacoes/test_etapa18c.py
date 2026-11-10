"""Etapa 18c: sobras de alocações/pessoas e refatoração com o comportamento travado."""

from contextlib import nullcontext
from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.financeiro import services as financeiro
from apps.financeiro.models import (
    AplicacaoRecebimento,
    Cobranca,
    MovimentacaoCaucao,
    MovimentoCredito,
)
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente

HOJE = timezone.localdate()
INICIO_DO_MES = HOJE.replace(day=1)
MES_PASSADO = INICIO_DO_MES - timedelta(days=3)


def _alocacao(placa, nome, cpf, inicio, **extra):
    veiculo = Veiculo.objects.create(placa=placa, marca_modelo="Gol", km_atual=50_000)
    cliente = Cliente.objects.create(nome=nome, cpf_cnpj=cpf)
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=inicio,
        valor_semanal=Decimal("650.00"),
        dia_vencimento=inicio.weekday(),
        km_entrega=50_000,
        **extra,
    )


def _aluguel(alocacao, vencimento, valor="650.00"):
    return Cobranca.objects.create(
        cliente=alocacao.cliente,
        alocacao=alocacao,
        origem=Cobranca.Origem.ALUGUEL,
        descricao=f"Aluguel {vencimento:%d/%m}",
        valor=Decimal(valor),
        vencimento=vencimento,
    )


def _pagar(cobranca, valor, data):
    return financeiro.registrar_recebimento(
        cobranca.cliente, data, Decimal(valor), "pix", [(cobranca, Decimal(valor))]
    )


# --- sobra 2: encerrar não regrava status intermediário nas semanas canceladas ---------


def _cenario_misto(placa, nome, cpf):
    """Encerrar retroativo com semanas pagas neste mês, no mês passado e pela caução."""
    inicio = INICIO_DO_MES - timedelta(days=20)
    alocacao = _alocacao(placa, nome, cpf, inicio)
    caucao = financeiro.abrir_caucao(alocacao, Decimal("1000.00"), MES_PASSADO, "pix")
    semanas = [_aluguel(alocacao, inicio + timedelta(days=7 * k)) for k in range(6)]
    _pagar(semanas[0], "650.00", MES_PASSADO)  # usada: fica como está
    _pagar(semanas[1], "300.00", MES_PASSADO)  # usada, parcial
    _pagar(semanas[2], "650.00", HOJE)  # não usada, paga neste mês
    _pagar(semanas[3], "300.00", MES_PASSADO)  # não usada, metade no mês passado
    _pagar(semanas[3], "350.00", HOJE)  # ... e metade neste mês
    financeiro.descontar_da_caucao(caucao, semanas[4], Decimal("650.00"), HOJE)
    # semanas[5]: não usada, sem pagamento
    return alocacao, inicio + timedelta(days=8)


def _retrato(alocacao):
    """Estado final do encerramento, sem ids nem placa (para comparar dois cenários)."""
    placa = alocacao.veiculo.placa
    cobrancas = [
        (c.vencimento, c.status, c.valor, c.observacoes)
        for c in Cobranca.objects.filter(alocacao=alocacao).order_by("vencimento")
    ]
    creditos = [
        (m.tipo, m.valor, m.data, m.observacoes.replace(placa, "PLACA"))
        for m in MovimentoCredito.objects.filter(cliente=alocacao.cliente).order_by("pk")
    ]
    caucao = [
        (m.tipo, m.valor, m.data, m.cobranca.vencimento if m.cobranca else None)
        for m in MovimentacaoCaucao.objects.filter(caucao__alocacao=alocacao).order_by("pk")
    ]
    aplicacoes = sorted(
        (a.cobranca.vencimento, a.valor)
        for a in AplicacaoRecebimento.objects.filter(cobranca__alocacao=alocacao)
    )
    alocacao.cliente.refresh_from_db()
    return {
        "cobrancas": cobrancas,
        "creditos": creditos,
        "caucao": caucao,
        "aplicacoes": aplicacoes,
        "acerto": alocacao.acerto_semanas_nao_usadas,
        "cliente": alocacao.cliente.status,
    }


def _versoes_do_encerrar(alocacao, desde):
    return list(
        Cobranca.history.filter(alocacao_id=alocacao.pk, history_id__gt=desde)
        .order_by("history_id")
        .values_list("vencimento", "status")
    )


def _ultima_versao():
    ultima = Cobranca.history.order_by("-history_id").first()
    return ultima.history_id if ultima else 0


def test_encerrar_cenario_misto_da_o_mesmo_resultado_sem_status_intermediario(db, monkeypatch):
    # Antes: cada pagamento apagado fazia o signal regravar a semana (atrasado/pendente)
    # e logo depois o encerrar gravava cancelada. O resultado final tem de ser o mesmo.
    velha, termino_velha = _cenario_misto("AAA1A11", "Antes", "529.982.247-25")
    nova, termino_nova = _cenario_misto("BBB2B22", "Depois", "111.444.777-35")

    import apps.financeiro.signals as sinais

    with monkeypatch.context() as m:
        m.setattr(sinais, "sem_recalculo_automatico", nullcontext)
        desde = _ultima_versao()
        with CaptureQueriesContext(connection) as antes:
            velha.encerrar(termino_velha, km_devolucao=51_000)
        versoes_velha = _versoes_do_encerrar(velha, desde)

    desde = _ultima_versao()
    with CaptureQueriesContext(connection) as depois:
        nova.encerrar(termino_nova, km_devolucao=51_000)
    versoes_nova = _versoes_do_encerrar(nova, desde)

    assert _retrato(nova) == _retrato(velha)
    # cada semana mexida ganha uma só versão, já com o status final
    finais = {c.vencimento: c.status for c in Cobranca.objects.filter(alocacao=nova)}
    assert all(status == finais[venc] for venc, status in versoes_nova)
    assert len(versoes_nova) == len({venc for venc, _ in versoes_nova})
    assert len(versoes_nova) < len(versoes_velha)
    assert len(depois) < len(antes)
    print(f"encerrar misto: {len(antes)} -> {len(depois)} consultas")


def test_encerrar_cenario_misto_resultado_travado(db):
    alocacao, termino = _cenario_misto("CCC3C33", "Travado", "529.982.247-25")
    alocacao.encerrar(termino, km_devolucao=51_000)
    retrato = _retrato(alocacao)
    status = [s for _, s, _, _ in retrato["cobrancas"]]
    valores = [v for _, _, v, _ in retrato["cobrancas"]]
    assert status == ["pago", "atrasado", "cancelada", "pago", "cancelada", "cancelada"]
    assert valores[3] == Decimal("300.00")  # baixada ao que foi pago no mês passado
    assert retrato["acerto"] == {
        "credito": Decimal("1000.00"),
        "caucao": Decimal("650.00"),
        "mes_anterior": Decimal("300.00"),
    }
    assert sum(v for _, v, _, _ in [c for c in retrato["creditos"] if c[0] == "entrada"]) == (
        Decimal("1000.00")
    )
    # o desconto da caução voltou: só o depósito fica
    assert [t for t, *_ in retrato["caucao"]] == ["deposito"]
    assert retrato["aplicacoes"] == [
        (alocacao.data_inicio, Decimal("650.00")),
        (alocacao.data_inicio + timedelta(days=7), Decimal("300.00")),
        (alocacao.data_inicio + timedelta(days=21), Decimal("300.00")),
    ]
