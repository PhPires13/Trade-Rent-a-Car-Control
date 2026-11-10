"""Etapa 18b — relatórios: encargo por atraso pago junto com o aluguel.

Achado do navegador: "a planilha Frota conta o encargo como aluguel" e "o DAS
chama o encargo de (ND)". Conferido com as regras (docs.md §4.3, quadro da
classificação fiscal): o encargo é receita do carro (não é aluguel, mas é
dinheiro que o carro trouxe) e recebe nota de débito, fora da base do DAS.
Então nos relatórios a coluna "Receita" da aba Frota soma os dois, e só o
aluguel entra na base do DAS — o encargo sai como pagamento diverso (ND)."""

from datetime import date
from decimal import Decimal

import pytest

from apps.alocacoes.models import Alocacao
from apps.financeiro.models import AplicacaoRecebimento, Cobranca, Recebimento
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente
from apps.relatorios import services, views


@pytest.fixture
def aluguel_e_encargo_pagos(db):
    cliente = Cliente.objects.create(nome="Cliente 19", cpf_cnpj="111.222.333-19")
    veiculo = Veiculo.objects.create(placa="TST1E11", marca_modelo="Onix")
    alocacao = Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 9, 1),
        valor_semanal=Decimal("700.00"),
        km_entrega=10_000,
    )
    recebimento = Recebimento.objects.create(
        cliente=cliente, data=date(2026, 10, 6), valor=Decimal("2840.00")
    )
    for origem, valor in (
        (Cobranca.Origem.ALUGUEL, Decimal("2800.00")),
        (Cobranca.Origem.ENCARGO, Decimal("40.00")),
    ):
        cobranca = Cobranca.objects.create(
            cliente=cliente,
            alocacao=alocacao,
            origem=origem,
            descricao=str(origem.label),
            valor=valor,
            vencimento=date(2026, 10, 6),
        )
        AplicacaoRecebimento.objects.create(recebimento=recebimento, cobranca=cobranca, valor=valor)
    return veiculo


def test_aba_frota_receita_soma_aluguel_e_encargo(aluguel_e_encargo_pagos):
    _, colunas, linhas = views._planilha_frota(2026, 10)
    linha = next(linha for linha in linhas if linha[0] == "TST1E11")
    assert linha[colunas.index("Receita")] == 2840.0


def test_encargo_fica_fora_da_base_do_das(aluguel_e_encargo_pagos):
    receitas = services.receitas_do_mes(2026, 10)
    assert receitas["locacao"] == Decimal("2800.00")
    assert receitas["diversos"] == {Cobranca.Origem.ENCARGO.label: Decimal("40.00")}

    _, _, linhas = views._planilha_receitas(2026, 10)
    por_grupo = {linha[0]: (linha[1], linha[2]) for linha in linhas}
    assert por_grupo["Receita de locação (fatura) — BASE DO DAS"] == (2800.0, "Sim")
    # encargo recebe nota de débito (docs.md §4.3) e não paga DAS
    assert por_grupo["Pagamentos diversos — Encargo por atraso (ND)"] == (40.0, "Não")


def test_aba_das_da_contabilidade_separa_encargo(aluguel_e_encargo_pagos):
    _, _, linhas = views._planilha_das(2026, 10)
    das = {linha[0]: (linha[1], linha[2]) for linha in linhas}
    assert das["Receita de locação (fatura)"] == (Decimal("2800.00"), "Sim")
    assert das["Pagamentos diversos — Encargo por atraso (ND)"] == (Decimal("40.00"), "Não")


def test_grafico_e_resumo_contam_aluguel_e_encargo(aluguel_e_encargo_pagos):
    """A barra "Receita" do gráfico, o "entrou" do resumo e o total da planilha
    batem: aluguel + encargo; a base do DAS fica só com o aluguel."""
    receitas = services.receitas_do_mes(2026, 10)
    assert receitas["total_receitas"] == Decimal("2840.00")
    resumo = services.resumo_do_mes(receitas, services.despesas_do_mes(2026, 10))
    assert resumo["entrou"] == Decimal("2840.00")
    assert resumo["base_das"] == Decimal("2800.00")
    pontos = {p["rotulo"]: p["receita"] for p in services.serie_mensal(2026, 10)}
    assert pontos["10/2026"] == 2840.0


def test_encargo_descontado_da_caucao_fica_fora_do_das_e_conta_na_frota(db):
    """Encargo quitado com a caução: mesmo efeito de pagamento (docs.md §4.3) —
    receita do carro na aba Frota, fora da base do DAS."""
    from apps.financeiro.models import Caucao, MovimentacaoCaucao

    cliente = Cliente.objects.create(nome="Cliente 20", cpf_cnpj="111.222.333-20")
    veiculo = Veiculo.objects.create(placa="TST2E22", marca_modelo="HB20")
    alocacao = Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 9, 1),
        valor_semanal=Decimal("700.00"),
        km_entrega=10_000,
    )
    caucao = Caucao.objects.create(alocacao=alocacao)
    MovimentacaoCaucao.objects.create(
        caucao=caucao,
        tipo=MovimentacaoCaucao.Tipo.RECEBIMENTO,
        valor=Decimal("1000.00"),
        data=date(2026, 9, 1),
    )
    encargo = Cobranca.objects.create(
        cliente=cliente,
        alocacao=alocacao,
        origem=Cobranca.Origem.ENCARGO,
        descricao="Encargo por atraso",
        valor=Decimal("40.00"),
        vencimento=date(2026, 10, 6),
    )
    MovimentacaoCaucao.objects.create(
        caucao=caucao,
        tipo=MovimentacaoCaucao.Tipo.DESCONTO,
        valor=Decimal("40.00"),
        data=date(2026, 10, 7),
        cobranca=encargo,
    )

    receitas = services.receitas_do_mes(2026, 10)
    assert receitas["locacao"] == Decimal("0")
    assert receitas["diversos"] == {Cobranca.Origem.ENCARGO.label: Decimal("40.00")}

    _, colunas, linhas = views._planilha_frota(2026, 10)
    linha = next(linha for linha in linhas if linha[0] == "TST2E22")
    assert linha[colunas.index("Receita")] == 40.0


def test_tela_de_relatorios_mostra_encargo_como_diverso(usuario_logado, aluguel_e_encargo_pagos):
    resposta = usuario_logado.get("/relatorios/?mes=2026-10")
    assert resposta.status_code == 200
    html = resposta.content.decode()
    assert "Pagamentos diversos (ND)" in html
    assert "R$ 40,00" in html
    assert "R$ 2.800,00" in html


def test_exportacao_da_contabilidade_com_encargo_nao_quebra(
    usuario_logado, aluguel_e_encargo_pagos
):
    resposta = usuario_logado.get("/relatorios/?exportar=tudo&mes=2026-10")
    assert resposta.status_code == 200
    assert resposta["Content-Type"].startswith("application/vnd.openxmlformats")
