"""Revisão adversarial da UX de multas: valores negativos, ND congelada,
cliques repetidos/abas antigas, parâmetros adulterados e consultas da lista."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.frota.models import Veiculo
from apps.multas import services
from apps.multas.models import Multa
from apps.pessoas.models import Cliente

HOJE = timezone.localdate()


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


@pytest.fixture
def veiculo(cliente):
    veiculo = Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol")
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


def test_valor_negativo_e_recusado_na_nova_e_na_nic(usuario_logado, veiculo):
    resposta = usuario_logado.post(
        "/multas/nova/",
        {
            "veiculo": veiculo.pk,
            "data_infracao": (HOJE - timedelta(days=3)).isoformat(),
            "valor": "-50,00",
            "tipo_condutor": "cliente",
            "responsavel": "cliente",
        },
    )
    assert resposta.status_code == 200
    assert "não pode ser negativo" in resposta.content.decode()
    assert not Multa.objects.exists()

    vencida = _multa(veiculo, fici_prazo=HOJE - timedelta(days=1))
    usuario_logado.post(f"/multas/{vencida.pk}/nic/", {"valor": "-195,30"})
    assert not Multa.objects.filter(multa_origem_nic=vencida).exists()
    vencida.refresh_from_db()
    assert vencida.fici_status == Multa.Fici.PENDENTE  # nada foi gravado pela metade


def test_editar_nao_muda_valor_nem_cliente_de_multa_em_nd(usuario_logado, veiculo, cliente):
    multa = _multa(veiculo, descricao="Cobrada")
    nd = services.gerar_nd_de_multas(cliente, [multa], HOJE)
    resposta = usuario_logado.post(
        f"/multas/{multa.pk}/editar/", _dados_form(multa, valor="150,00")
    )
    assert resposta.status_code == 200
    assert f"ND {nd.numero:03d}" in resposta.content.decode()
    multa.refresh_from_db()
    assert multa.valor == Decimal("100.00")

    outro = Cliente.objects.create(nome="Bia", cpf_cnpj="999.888.777-66")
    resposta = usuario_logado.post(
        f"/multas/{multa.pk}/editar/", _dados_form(multa, cliente=outro.pk)
    )
    assert resposta.status_code == 200
    multa.refresh_from_db()
    assert multa.cliente == cliente

    # o resto continua editável (mesmo valor digitado no formato brasileiro)
    resposta = usuario_logado.post(
        f"/multas/{multa.pk}/editar/",
        _dados_form(multa, pagamento="pago", pago_por="Empresa", observacoes="paga no app"),
    )
    assert resposta.status_code == 302
    multa.refresh_from_db()
    assert multa.pagamento == Multa.Pagamento.PAGO and multa.observacoes == "paga no app"


def test_voltar_para_a_pagar_limpa_quem_pagou(usuario_logado, veiculo):
    multa = _multa(veiculo, pagamento=Multa.Pagamento.PAGO, pago_por="Cliente")
    resposta = usuario_logado.post(
        f"/multas/{multa.pk}/editar/", _dados_form(multa, pagamento="pendente")
    )
    assert resposta.status_code == 302
    multa.refresh_from_db()
    assert multa.pagamento == Multa.Pagamento.PENDENTE
    assert multa.pago_por == ""


def test_aba_antiga_nao_desfaz_nic_nem_registra_nic_de_indicada(usuario_logado, veiculo):
    vencida = _multa(veiculo, fici_prazo=HOJE - timedelta(days=5))
    services.registrar_nic(vencida, valor=Decimal("195.30"))
    usuario_logado.post(f"/multas/{vencida.pk}/fici/")
    vencida.refresh_from_db()
    assert vencida.fici_status == Multa.Fici.PRAZO_PERDIDO
    assert vencida.fici_data_indicacao is None

    indicada = _multa(
        veiculo,
        fici_prazo=HOJE - timedelta(days=5),
        fici_status=Multa.Fici.INDICADO,
        fici_data_indicacao=HOJE - timedelta(days=10),
    )
    resposta = usuario_logado.post(f"/multas/{indicada.pk}/nic/", {"valor": "195,30"}, follow=True)
    assert "já foi indicado" in resposta.content.decode()
    assert not Multa.objects.filter(multa_origem_nic=indicada).exists()
    # clicar duas vezes em "Condutor indicado" não troca a data já gravada
    usuario_logado.post(f"/multas/{indicada.pk}/fici/")
    indicada.refresh_from_db()
    assert indicada.fici_data_indicacao == HOJE - timedelta(days=10)


def test_multa_sem_valor_indicada_continua_em_pagar(usuario_logado, veiculo):
    # etapa 17 (extra): sem valor ainda não está paga — não pode ir para "Concluídas"
    _multa(veiculo, valor=None, fici_status=Multa.Fici.INDICADO)
    contas = {a["chave"]: a["total"] for a in usuario_logado.get("/multas/").context["atalhos"]}
    assert contas == {"indicar": 0, "pagar": 1, "cobrar": 0, "concluidas": 0}


def test_parametros_adulterados_nao_dao_erro(usuario_logado, veiculo):
    _multa(veiculo)
    for params in (
        {"resultado": "lixo"},
        {"pagina": "abc"},
        {"pagina": "-1"},
        {"cliente": "1 OR 1=1"},
        {"tarefa": "<script>"},
    ):
        assert usuario_logado.get("/multas/", params).status_code == 200
    resposta = usuario_logado.get(
        "/multas/quem-estava/",
        {
            "veiculo": "abc",
            "data_infracao": "2026-02-30",
            "cliente": "x",
            "condutor_autorizado": "-1",
        },
    )
    assert resposta.status_code == 200
    assert usuario_logado.get("/multas/999999/editar/").status_code == 404


def test_quem_estava_avisa_cliente_diferente(usuario_logado, veiculo):
    bia = Cliente.objects.create(nome="Bia", cpf_cnpj="999.888.777-66")
    html = usuario_logado.get(
        "/multas/quem-estava/",
        {
            "veiculo": veiculo.pk,
            "data_infracao": (HOJE - timedelta(days=3)).isoformat(),
            "cliente": bia.pk,
        },
    ).content.decode()
    assert "não estava com o carro nessa data" in html


def test_acoes_por_get_nao_gravam_nada(usuario_logado, veiculo):
    multa = _multa(veiculo)
    usuario_logado.get(f"/multas/{multa.pk}/paga/")
    usuario_logado.get(f"/multas/{multa.pk}/fici/")
    multa.refresh_from_db()
    assert multa.pagamento == Multa.Pagamento.PENDENTE
    assert multa.fici_status == Multa.Fici.PENDENTE


def test_lista_sem_consulta_por_linha(usuario_logado, veiculo, django_assert_max_num_queries):
    for dias in range(1, 6):
        origem = _multa(veiculo, fici_prazo=HOJE - timedelta(days=dias))
        services.registrar_nic(origem, valor=Decimal("195.30"))
    usuario_logado.get("/multas/")  # aquece sessão/caches
    with django_assert_max_num_queries(8):
        resposta = usuario_logado.get("/multas/")
    assert resposta.status_code == 200
    assert "Multa NIC (empresa)" in resposta.content.decode()
