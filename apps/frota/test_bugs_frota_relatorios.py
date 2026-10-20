"""Correções da frente frota-relatórios: C09, C10, C12, C14, C26, D06, D07 (ficha e cadastro)."""

import io
from datetime import date
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from PIL import Image

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.frota import desmobilizacao
from apps.frota.alertas import pendencias_do_veiculo, vigencias_a_vencer
from apps.frota.models import PagamentoIpva, Veiculo
from apps.frota.views import VeiculoForm
from apps.manutencao.models import Manutencao
from apps.multas.models import Multa
from apps.pessoas.models import Cliente
from apps.sinistros.models import Sinistro


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(
        placa="QXQ6C10",
        marca_modelo="Gol",
        km_atual=95_000,
        data_aquisicao=date(2025, 1, 10),
        valor_compra=Decimal("40000.00"),
        mensalidade_protecao=Decimal("190.00"),
    )


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Ana", cpf_cnpj="111.222.333-44")


@pytest.fixture
def alocacao(veiculo, cliente):
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=95_000,
    )


def _dados_form(veiculo, **extra):
    return {
        "placa": veiculo.placa,
        "marca_modelo": veiculo.marca_modelo,
        "uso": veiculo.uso,
        "km_atual": veiculo.km_atual,
        "chave_reserva": veiculo.chave_reserva,
        **extra,
    }


# --- C09: proteção da ficha do mês da compra ao da venda, os dois inclusive ----------


def test_c09_carro_vendido_para_de_pagar_protecao_na_venda(veiculo):
    veiculo.data_venda = date(2026, 3, 1)
    veiculo.valor_venda = Decimal("30000.00")
    veiculo.status = Veiculo.Status.VENDIDO
    veiculo.save()
    ficha = desmobilizacao.montar_ficha(veiculo, hoje=date(2026, 10, 20))
    # jan/2025 a mar/2026 = 15 meses (o relatório conta o mês da venda também)
    assert ficha.despesa_protecao_estimada == Decimal("190.00") * 15
    # o resultado final não cai a cada mês que passa depois da venda
    depois = desmobilizacao.montar_ficha(veiculo, hoje=date(2027, 5, 1))
    assert depois.despesa_protecao_estimada == ficha.despesa_protecao_estimada


def test_c09_carro_na_frota_conta_o_mes_da_compra_e_o_atual(db):
    carro = Veiculo.objects.create(
        placa="ABC1D23",
        marca_modelo="Onix",
        data_aquisicao=date(2026, 1, 15),
        mensalidade_protecao=Decimal("190.00"),
    )
    ficha = desmobilizacao.montar_ficha(carro, hoje=date(2026, 10, 20))
    assert ficha.despesa_protecao_estimada == Decimal("1900.00")  # jan..out = 10 meses


# --- C10: multa anulada não é despesa da ficha ----------------------------------------


def test_c10_multa_anulada_sai_da_despesa_da_ficha(veiculo):
    for resultado in (
        Multa.Resultado.ADVERTENCIA,
        Multa.Resultado.CANCELADA,
        Multa.Resultado.NAO_EXIGIVEL,
    ):
        Multa.objects.create(
            veiculo=veiculo,
            data_infracao=date(2026, 5, 3),
            valor=Decimal("195.23"),
            responsavel=Multa.Responsavel.EMPRESA,
            resultado=resultado,
        )
    Multa.objects.create(
        veiculo=veiculo,
        data_infracao=date(2026, 5, 4),
        valor=Decimal("88.38"),
        responsavel=Multa.Responsavel.EMPRESA,
    )
    ficha = desmobilizacao.montar_ficha(veiculo, hoje=date(2026, 10, 20))
    assert ficha.despesa_multas_empresa == Decimal("88.38")


# --- C12 / D06: IPVA de todos os anos na ficha --------------------------------------


def test_c12_ficha_desconta_o_ipva_pago_de_todos_os_anos(veiculo):
    PagamentoIpva.objects.create(
        veiculo=veiculo, ano=2025, valor=Decimal("1200.00"), pago_em=date(2025, 2, 10)
    )
    PagamentoIpva.objects.create(
        veiculo=veiculo, ano=2026, parcela=1, valor=Decimal("420.00"), pago_em=date(2026, 1, 15)
    )
    PagamentoIpva.objects.create(  # parcela ainda não paga: não é despesa
        veiculo=veiculo, ano=2026, parcela=2, valor=Decimal("420.00"), vencimento=date(2026, 2, 15)
    )
    ficha = desmobilizacao.montar_ficha(veiculo, hoje=date(2026, 10, 20))
    assert ficha.despesa_ipva == Decimal("1620.00")
    assert ficha.despesa_total == ficha.despesa_protecao_estimada + Decimal("1620.00")


def test_c12_tela_da_ficha_mostra_o_ipva(usuario_logado, veiculo):
    PagamentoIpva.objects.create(
        veiculo=veiculo, ano=2026, valor=Decimal("1234.00"), pago_em=date(2026, 1, 15)
    )
    html = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/ficha/").content.decode()
    assert "IPVA pago" in html
    assert "1.234,00" in html


# --- D07: cota da proteção só no sinistro -------------------------------------------


def test_d07_conserto_pago_pela_cota_nao_conta_em_dobro_na_ficha(veiculo):
    sinistro = Sinistro.objects.create(
        veiculo=veiculo,
        data=date(2026, 3, 5),
        envolvido="terceiro",
        acionou_protecao=True,
        data_evento=date(2026, 3, 5),
        franquia_valor=Decimal("1500.00"),
    )
    Manutencao.objects.create(  # a mesma cota, digitada de novo no conserto
        veiculo=veiculo,
        tipo="corretiva",
        data=date(2026, 3, 20),
        descricao="Funilaria do sinistro",
        origem_custo=Manutencao.OrigemCusto.PROTECAO,
        custo_real=Decimal("1500.00"),
        sinistro=sinistro,
    )
    Manutencao.objects.create(  # o que foi pago por fora entra
        veiculo=veiculo,
        tipo="corretiva",
        data=date(2026, 3, 21),
        descricao="Farol (por fora)",
        origem_custo=Manutencao.OrigemCusto.PARTICULAR,
        custo_real=Decimal("300.00"),
        sinistro=sinistro,
    )
    ficha = desmobilizacao.montar_ficha(veiculo, hoje=date(2026, 10, 20))
    assert ficha.despesa_franquias == Decimal("1500.00")
    assert ficha.despesa_manutencao == Decimal("300.00")


# --- C14: carro alugado não vira "Fora de locação" ----------------------------------


def test_c14_nao_marca_fora_de_locacao_com_alocacao_ativa(alocacao, veiculo):
    form = VeiculoForm(_dados_form(veiculo, uso=Veiculo.Uso.FORA_LOCACAO), instance=veiculo)
    assert not form.is_valid()
    assert "Encerre a alocação" in form.errors["uso"][0]


def test_c14_nao_marca_fora_de_locacao_com_troca_aberta(alocacao, db):
    substituto = Veiculo.objects.create(placa="RNB9J66", marca_modelo="Voyage", km_atual=50_000)
    TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=date(2026, 7, 10),
        km_retirada=50_000,
    )
    form = VeiculoForm(_dados_form(substituto, uso=Veiculo.Uso.FORA_LOCACAO), instance=substituto)
    assert not form.is_valid()
    assert "devolução" in form.errors["uso"][0]


def test_c14_carro_livre_pode_ir_para_fora_de_locacao(veiculo):
    form = VeiculoForm(_dados_form(veiculo, uso=Veiculo.Uso.FORA_LOCACAO), instance=veiculo)
    assert form.is_valid(), form.errors


# --- C26: foto escolhida num formulário com erro -------------------------------------


def _foto(nome="IMG_1234.png"):
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), "#F0C948").save(buffer, format="PNG")
    return SimpleUploadedFile(nome, buffer.getvalue(), content_type="image/png")


def test_c26_foto_nova_com_erro_no_form_avisa_e_nao_mostra_imagem_quebrada(
    usuario_logado, veiculo, settings, tmp_path
):
    settings.MEDIA_ROOT = tmp_path
    dados = _dados_form(veiculo, km_atual=10)  # KM menor que o registrado: erro
    dados["foto"] = _foto()
    resposta = usuario_logado.post(f"/frota/veiculo/{veiculo.pk}/editar/", dados)
    html = resposta.content.decode()
    assert resposta.status_code == 200
    assert "/media/IMG_1234.png" not in html
    assert "escolha a foto de novo antes de salvar" in html


def test_c26_foto_gravada_continua_aparecendo(usuario_logado, veiculo, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    veiculo.foto = _foto("antiga.png")
    veiculo.save()
    html = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/editar/").content.decode()
    assert veiculo.foto.url in html
    assert "escolha a foto de novo" not in html


# --- D06: IPVA ano a ano --------------------------------------------------------------


def test_d06_tela_de_ipva_lanca_ano_novo_sem_apagar_o_anterior(usuario_logado, veiculo):
    antigo = PagamentoIpva.objects.create(
        veiculo=veiculo, ano=2026, valor=Decimal("1200.00"), pago_em=date(2026, 1, 15)
    )
    url = f"/frota/veiculo/{veiculo.pk}/ipva/"
    assert usuario_logado.get(url).status_code == 200
    resposta = usuario_logado.post(
        url,
        {
            "pagamentos_ipva-TOTAL_FORMS": "2",
            "pagamentos_ipva-INITIAL_FORMS": "1",
            "pagamentos_ipva-0-id": str(antigo.pk),
            "pagamentos_ipva-0-veiculo": str(veiculo.pk),
            "pagamentos_ipva-0-ano": "2026",
            "pagamentos_ipva-0-parcela": "1",
            "pagamentos_ipva-0-valor": "1.200,00",
            "pagamentos_ipva-0-pago_em": "2026-01-15",
            "pagamentos_ipva-1-veiculo": str(veiculo.pk),
            "pagamentos_ipva-1-ano": "2027",
            "pagamentos_ipva-1-parcela": "1",
            "pagamentos_ipva-1-valor": "1.260,00",
            "pagamentos_ipva-1-vencimento": "2027-02-10",
        },
    )
    assert resposta.status_code == 302, resposta.context["formset"].errors
    assert list(veiculo.pagamentos_ipva.order_by("ano").values_list("ano", "valor", "pago_em")) == [
        (2026, Decimal("1200.00"), date(2026, 1, 15)),
        (2027, Decimal("1260.00"), None),
    ]


def test_d06_pago_sem_valor_nao_salva(usuario_logado, veiculo):
    resposta = usuario_logado.post(
        f"/frota/veiculo/{veiculo.pk}/ipva/",
        {
            "pagamentos_ipva-TOTAL_FORMS": "1",
            "pagamentos_ipva-INITIAL_FORMS": "0",
            "pagamentos_ipva-0-ano": "2026",
            "pagamentos_ipva-0-parcela": "1",
            "pagamentos_ipva-0-pago_em": "2026-01-15",
        },
    )
    assert resposta.status_code == 200
    assert not PagamentoIpva.objects.exists()


def test_d06_virada_do_ano_avisa_ipva_nao_cadastrado(veiculo):
    PagamentoIpva.objects.create(
        veiculo=veiculo, ano=2026, valor=Decimal("1200.00"), pago_em=date(2026, 1, 15)
    )
    hoje = date(2027, 1, 5)
    alertas = vigencias_a_vencer(hoje=hoje)
    assert any("IPVA 2027 do QXQ6C10 ainda não cadastrado" in a["descricao"] for a in alertas)
    pendencias = {p["texto"] for p in pendencias_do_veiculo(veiculo, hoje=hoje)}
    assert "IPVA 2027 ainda não cadastrado" in pendencias

    PagamentoIpva.objects.create(
        veiculo=veiculo, ano=2027, valor=Decimal("1260.00"), vencimento=date(2027, 3, 10)
    )
    alertas = vigencias_a_vencer(hoje=hoje)
    assert not any("ainda não cadastrado" in a["descricao"] for a in alertas)


def test_d06_parcelas_em_aberto_alertam_uma_a_uma(veiculo):
    for parcela, mes in ((1, 1), (2, 2), (3, 3)):
        PagamentoIpva.objects.create(
            veiculo=veiculo,
            ano=2027,
            parcela=parcela,
            valor=Decimal("420.00"),
            vencimento=date(2027, mes, 10),
            pago_em=date(2027, 1, 8) if parcela == 1 else None,
        )
    pendencias = [p["texto"] for p in pendencias_do_veiculo(veiculo, hoje=date(2027, 1, 20))]
    assert "IPVA 2027 — parcela 2 em aberto" in pendencias
    assert "IPVA 2027 em aberto" not in pendencias  # 1ª parcela paga
    assert "IPVA 2027 — parcela 3 em aberto" not in pendencias  # vence em mais de 30 dias


@pytest.mark.django_db(transaction=True)
def test_d06_migracao_copia_o_ipva_atual_e_os_anos_do_historico():
    executor = MigrationExecutor(connection)
    antes = [("frota", "0006_historicalveiculo_documento_veiculo_documento")]
    depois = [("frota", "0007_pagamento_ipva")]
    executor.migrate(antes)
    estado = executor.loader.project_state(antes).apps
    VeiculoAntigo = estado.get_model("frota", "Veiculo")
    HistoricoAntigo = estado.get_model("frota", "HistoricalVeiculo")
    carro = VeiculoAntigo.objects.create(
        placa="ABC1D23",
        marca_modelo="Gol",
        ipva_ano=2027,
        ipva_valor=Decimal("1260.00"),
        ipva_vencimento=date(2027, 2, 10),
    )
    # versão de 2026 que o cadastro sobrescreveu (fica no simple-history)
    HistoricoAntigo.objects.create(
        id=carro.pk,
        placa="ABC1D23",
        marca_modelo="Gol",
        uso="locacao",
        status="disponivel",
        km_atual=0,
        chave_reserva="duvida",
        ipva_ano=2026,
        ipva_valor=Decimal("1200.00"),
        ipva_pago_em=date(2026, 1, 15),
        history_date="2026-01-15T10:00:00Z",
        history_type="~",
    )
    VeiculoAntigo.objects.create(placa="SEM0I00", marca_modelo="HB20")  # sem IPVA nenhum

    executor = MigrationExecutor(connection)
    executor.migrate(depois)
    estado = executor.loader.project_state(depois).apps
    Pagamento = estado.get_model("frota", "PagamentoIpva")
    copiados = list(
        Pagamento.objects.order_by("ano").values_list(
            "veiculo__placa", "ano", "parcela", "valor", "vencimento", "pago_em"
        )
    )
    assert copiados == [
        ("ABC1D23", 2026, 1, Decimal("1200.00"), None, date(2026, 1, 15)),
        ("ABC1D23", 2027, 1, Decimal("1260.00"), date(2027, 2, 10), None),
    ]


def test_d06_ano_e_parcela_repetidos_dao_erro_e_nao_erro_500(usuario_logado, veiculo):
    """Revisão: a regra 'um registro por carro, ano e parcela' vira mensagem no formulário."""
    antigo = PagamentoIpva.objects.create(veiculo=veiculo, ano=2026, valor=Decimal("1200.00"))
    url = f"/frota/veiculo/{veiculo.pk}/ipva/"
    base = {
        "pagamentos_ipva-TOTAL_FORMS": "2",
        "pagamentos_ipva-INITIAL_FORMS": "1",
        "pagamentos_ipva-0-id": str(antigo.pk),
        "pagamentos_ipva-0-veiculo": str(veiculo.pk),
        "pagamentos_ipva-0-ano": "2026",
        "pagamentos_ipva-0-parcela": "1",
        "pagamentos_ipva-0-valor": "1.200,00",
        "pagamentos_ipva-1-veiculo": str(veiculo.pk),
        "pagamentos_ipva-1-ano": "2026",
        "pagamentos_ipva-1-parcela": "1",
        "pagamentos_ipva-1-valor": "300,00",
    }
    resposta = usuario_logado.post(url, base)
    assert resposta.status_code == 200
    assert PagamentoIpva.objects.count() == 1


def test_d06_valor_negativo_de_ipva_nao_salva(usuario_logado, veiculo):
    resposta = usuario_logado.post(
        f"/frota/veiculo/{veiculo.pk}/ipva/",
        {
            "pagamentos_ipva-TOTAL_FORMS": "1",
            "pagamentos_ipva-INITIAL_FORMS": "0",
            "pagamentos_ipva-0-ano": "2026",
            "pagamentos_ipva-0-parcela": "1",
            "pagamentos_ipva-0-valor": "-1.200,00",
            "pagamentos_ipva-0-pago_em": "2026-01-15",
        },
    )
    assert resposta.status_code == 200
    assert not PagamentoIpva.objects.exists()


def test_d06_tela_de_ipva_de_carro_inexistente_da_404(usuario_logado, db):
    assert usuario_logado.get("/frota/veiculo/999999/ipva/").status_code == 404


@pytest.mark.django_db(transaction=True)
def test_d06_migracao_nao_duplica_pagamento_quando_o_ano_foi_corrigido():
    """Revisão: ano digitado errado e corrigido depois não pode virar dois IPVAs pagos."""
    executor = MigrationExecutor(connection)
    antes = [("frota", "0006_historicalveiculo_documento_veiculo_documento")]
    depois = [("frota", "0007_pagamento_ipva")]
    executor.migrate(antes)
    estado = executor.loader.project_state(antes).apps
    VeiculoAntigo = estado.get_model("frota", "Veiculo")
    HistoricoAntigo = estado.get_model("frota", "HistoricalVeiculo")
    pago = {"ipva_valor": Decimal("1200.00"), "ipva_pago_em": date(2026, 1, 15)}
    carro = VeiculoAntigo.objects.create(placa="ABC1D23", marca_modelo="Gol", ipva_ano=2026, **pago)
    comum = {
        "id": carro.pk,
        "placa": "ABC1D23",
        "marca_modelo": "Gol",
        "uso": "locacao",
        "status": "disponivel",
        "km_atual": 0,
        "chave_reserva": "duvida",
        "history_type": "~",
    }
    HistoricoAntigo.objects.create(  # digitou 2025 por engano...
        ipva_ano=2025, history_date="2026-01-15T10:00:00Z", **pago, **comum
    )
    HistoricoAntigo.objects.create(  # ...e corrigiu para 2026 no mesmo dia
        ipva_ano=2026, history_date="2026-01-15T10:05:00Z", **pago, **comum
    )

    executor = MigrationExecutor(connection)
    executor.migrate(depois)
    estado = executor.loader.project_state(depois).apps
    Pagamento = estado.get_model("frota", "PagamentoIpva")
    assert list(Pagamento.objects.values_list("ano", "valor", "pago_em")) == [
        (2026, Decimal("1200.00"), date(2026, 1, 15))
    ]


def test_d06_linha_em_branco_nao_cria_ipva_vazio(usuario_logado, veiculo):
    """Revisão: salvar a tela sem preencher a linha nova (ano/parcela já vêm sugeridos)."""
    resposta = usuario_logado.post(
        f"/frota/veiculo/{veiculo.pk}/ipva/",
        {
            "pagamentos_ipva-TOTAL_FORMS": "1",
            "pagamentos_ipva-INITIAL_FORMS": "0",
            "pagamentos_ipva-0-ano": str(date.today().year),
            "pagamentos_ipva-0-parcela": "1",
        },
    )
    assert resposta.status_code == 302
    assert not PagamentoIpva.objects.exists()
