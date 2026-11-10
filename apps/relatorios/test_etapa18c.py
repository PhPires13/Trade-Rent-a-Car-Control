"""Etapa 18c — Relatórios: tela e arquivo da contabilidade sem crescer com os
dados, o "baixar tudo" sem refazer o DAS três vezes e uma rede de segurança
que confere as três cópias das mesmas contas (série, mês a mês e ficha do carro).
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.alocacoes.models import Alocacao
from apps.financeiro import services as financeiro
from apps.financeiro.models import Cobranca
from apps.frota.models import PagamentoIpva, Veiculo
from apps.manutencao.models import Manutencao
from apps.multas.models import Multa
from apps.pessoas.models import Cliente
from apps.sinistros.models import AuxilioMotorista, Sinistro

HOJE = date(2026, 9, 20)


def _carro(i, **campos):
    dados = {
        "placa": f"TST{i:04d}",
        "marca_modelo": "Gol",
        "km_atual": 50_000,
        "data_aquisicao": date(2026, 1, 1) + timedelta(days=3 * i),
        "valor_compra": Decimal("40000.00"),
        "custos_entrada": Decimal("1500.00") + i,
        "mensalidade_protecao": Decimal("304.00"),
    }
    dados.update(campos)
    return Veiculo.objects.create(**dados)


def _montar_base(n):
    """n carros de locação, cada um com todas as fontes dos relatórios, e dois
    carros pessoais com gastos (que ficam fora)."""
    for i in range(n):
        carro = _carro(i)
        cliente = Cliente.objects.create(nome=f"Cliente {i}", cpf_cnpj=f"{i:03d}.222.333-44")
        alocacao = Alocacao.objects.create(
            veiculo=carro,
            cliente=cliente,
            data_inicio=date(2026, 6, 1) + timedelta(days=i % 20),
            valor_semanal=Decimal("650.00"),
            km_entrega=50_000,
        )
        caucao = financeiro.abrir_caucao(
            alocacao, valor_recebido=Decimal("1000.00"), data=alocacao.data_inicio
        )
        financeiro.gerar_cobrancas_semanais(hoje=HOJE, alocacao=alocacao)
        semanas = list(Cobranca.objects.filter(alocacao=alocacao).order_by("vencimento"))
        # 1ª semana pela caução, as do meio por Pix (uma com sobra que vira
        # crédito), as últimas ficam em aberto (recebíveis)
        financeiro.descontar_da_caucao(
            caucao, semanas[0], semanas[0].valor, semanas[0].vencimento, hoje=HOJE
        )
        for k, semana in enumerate(semanas[1:-2], start=1):
            sobra = Decimal("50.00") if k == 1 else Decimal("0")
            financeiro.registrar_recebimento(
                cliente,
                semana.vencimento,
                semana.valor + sobra,
                "pix",
                [(semana, semana.valor)],
                hoje=HOJE,
            )
        Manutencao.objects.create(
            veiculo=carro,
            tipo="corretiva",
            data=date(2026, 7, 1) + timedelta(days=i % 28),
            descricao=f"Conserto {i}",
            custo_real=Decimal("200.00") + i,
        )
        sinistro = Sinistro.objects.create(
            veiculo=carro,
            data=date(2026, 8, 1) + timedelta(days=i % 25),
            envolvido="terceiro",
            acionou_protecao=True,
            franquia_valor=Decimal("1500.00"),
            data_evento=None if i % 2 else date(2026, 8, 2) + timedelta(days=i % 25),
        )
        AuxilioMotorista.objects.create(
            sinistro=sinistro,
            valor=Decimal("700.00"),
            status=AuxilioMotorista.Status.RECEBIDO,
            data_recebimento=date(2026, 8, 20),
        )
        Multa.objects.create(
            veiculo=carro,
            data_infracao=date(2026, 7, 5) + timedelta(days=i % 20),
            valor=Decimal("130.16"),
            responsavel=Multa.Responsavel.EMPRESA,
        )
        PagamentoIpva.objects.create(
            veiculo=carro,
            ano=2026,
            parcela=1,
            valor=Decimal("900.00") + i,
            pago_em=date(2026, 2, 10) + timedelta(days=i),
        )
    # um carro vendido (receita e custo de venda), sem proteção depois da venda
    _carro(
        n + 1,
        data_venda=date(2026, 8, 15),
        valor_venda=Decimal("35000.00"),
        custos_venda=Decimal("800.00"),
        status=Veiculo.Status.VENDIDO,
    )
    for j in range(2):
        pessoal = _carro(n + 10 + j, uso=Veiculo.Uso.FORA_LOCACAO)
        Manutencao.objects.create(
            veiculo=pessoal,
            tipo="corretiva",
            data=date(2026, 8, 3),
            descricao="Pessoal",
            custo_real=Decimal("999.00"),
        )


def _consultas(funcao):
    with CaptureQueriesContext(connection) as contexto:
        resultado = funcao()
    return len(contexto.captured_queries), resultado


@pytest.mark.parametrize("n", [3, 12])
def test_medicao(usuario_logado, n):
    _montar_base(n)
    tela, _ = _consultas(lambda: usuario_logado.get("/relatorios/", {"mes": "2026-08"}))
    tudo, _ = _consultas(
        lambda: usuario_logado.get("/relatorios/", {"mes": "2026-08", "exportar": "tudo"})
    )
    print(f"\nn={n} tela={tela} tudo={tudo}")
