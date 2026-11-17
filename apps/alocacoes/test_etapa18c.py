"""Etapa 18c: sobras de alocações/pessoas e refatoração com o comportamento travado."""

import re
from contextlib import nullcontext
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.alocacoes.models import ALOCACAO_JA_ENCERRADA, Alocacao, TrocaTemporaria, Vistoria
from apps.financeiro import services as financeiro
from apps.financeiro.models import (
    AplicacaoRecebimento,
    Caucao,
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
    assert [t for t, *_ in retrato["caucao"]] == ["recebimento"]
    assert retrato["aplicacoes"] == [
        (alocacao.data_inicio, Decimal("650.00")),
        (alocacao.data_inicio + timedelta(days=7), Decimal("300.00")),
        (alocacao.data_inicio + timedelta(days=21), Decimal("300.00")),
    ]


# --- sobra 1: refazer as semanas da troca trava o cliente antes de comparar o pago ------


def _travas_durante(funcao, monkeypatch):
    """Modelos travados (select_for_update), na ordem em que foram travados."""
    from django.db.models.query import QuerySet

    chamadas = []
    original = QuerySet.select_for_update

    def espiao(self, *args, **kwargs):
        chamadas.append(self.model)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", espiao)
    funcao()
    return chamadas


def _troca_com_ajuste(placa_sub="SUB1A11"):
    alocacao = _alocacao("PRI1A11", "Rita", "529.982.247-25", HOJE - timedelta(days=2))
    financeiro.gerar_cobrancas_semanais(alocacao=alocacao)
    substituto = Veiculo.objects.create(placa=placa_sub, marca_modelo="HB20", km_atual=30_000)
    troca = TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=HOJE,
        km_retirada=30_000,
        valor_semanal_ajustado=Decimal("840.00"),
    )
    return alocacao, troca


def test_refazer_semanas_da_troca_trava_o_cliente_antes_das_cobrancas(db, monkeypatch):
    from apps.alocacoes.services import refazer_semanas_da_troca

    alocacao, troca = _troca_com_ajuste()
    semana = alocacao.cobrancas.get(origem=Cobranca.Origem.ALUGUEL)
    assert semana.valor != Decimal("650.00")  # a troca dividiu a semana
    troca.valor_semanal_ajustado = None
    TrocaTemporaria.objects.filter(pk=troca.pk).update(valor_semanal_ajustado=None)
    travas = _travas_durante(lambda: refazer_semanas_da_troca(troca), monkeypatch)
    assert travas and travas[0] is Cliente
    semana.refresh_from_db()
    assert semana.valor == Decimal("650.00")  # resultado igual: volta ao preço do contrato


def test_tirar_o_ajuste_no_admin_continua_refazendo_a_semana(db):
    alocacao, troca = _troca_com_ajuste()
    troca.valor_semanal_ajustado = None
    troca.save()
    assert [c.valor for c in troca.semanas_reajustadas] == [Decimal("650.00")]
    assert alocacao.cobrancas.get(origem=Cobranca.Origem.ALUGUEL).valor == Decimal("650.00")


# --- sobra 3: ?motivo= da manutenção/ficha do carro já chega preenchido --------------------


def test_troca_nova_preenche_o_motivo_do_link(usuario_logado):
    alocacao = _alocacao("PRI2A22", "Rita", "529.982.247-25", HOJE - timedelta(days=2))
    url = reverse("alocacoes:troca_nova", args=[alocacao.pk])
    resposta = usuario_logado.get(url, {"motivo": "Manutenção do carro principal"})
    assert resposta.context["form"]["motivo"].value() == "Manutenção do carro principal"
    assert 'value="Manutenção do carro principal"' in resposta.content.decode()
    # sem o parâmetro, o campo segue vazio; texto enorme é cortado no tamanho do campo
    assert not usuario_logado.get(url).context["form"]["motivo"].value()
    longo = usuario_logado.get(url, {"motivo": "x" * 500}).context["form"]["motivo"].value()
    assert longo == "x" * 200


def test_troca_nova_envio_usa_o_motivo_digitado_nao_o_do_link(usuario_logado):
    alocacao = _alocacao("PRI3A33", "Rita", "529.982.247-25", HOJE - timedelta(days=2))
    substituto = Veiculo.objects.create(placa="SUB3A33", marca_modelo="HB20", km_atual=30_000)
    url = reverse("alocacoes:troca_nova", args=[alocacao.pk]) + "?motivo=Oficina"
    usuario_logado.post(
        url,
        {
            "veiculo_substituto": substituto.pk,
            "data_retirada": HOJE.isoformat(),
            "km_retirada": "30000",
            "motivo": "Batida",
        },
    )
    assert TrocaTemporaria.objects.get().motivo == "Batida"


# --- sobra 4: __str__ sem derrubar o Histórico quando o carro/cliente sumiu ---------------


def test_str_de_alocacao_troca_e_vistoria_com_carro_e_cliente_apagados(db):
    alocacao = Alocacao(veiculo_id=987_654, cliente_id=987_654, status="encerrada")
    assert str(alocacao) == "carro apagado → cliente apagado (Encerrada)"
    troca = TrocaTemporaria(alocacao=alocacao, veiculo_substituto_id=987_654, data_retirada=HOJE)
    assert str(troca) == f"cliente apagado com carro apagado desde {HOJE:%d/%m/%Y}"
    sem_alocacao = TrocaTemporaria(
        alocacao_id=987_654, veiculo_substituto_id=987_654, data_retirada=HOJE
    )
    assert "cliente apagado" in str(sem_alocacao)
    vistoria = Vistoria(alocacao_id=987_654, tipo="entrada", data=HOJE)
    assert str(vistoria) == f"Entrega ao motorista — carro apagado em {HOJE:%d/%m/%Y}"


def test_str_de_alocacao_normal_continua_igual(db):
    alocacao = _alocacao("STR1A11", "Rita", "529.982.247-25", HOJE)
    assert str(alocacao) == "STR1A11 → Rita (Ativa)"


# --- sobra 5: falha ao lançar a 1ª semana não vira erro 500 na nova alocação -------------


def _dados_nova(veiculo, cliente, inicio):
    return {
        "veiculo": veiculo.pk,
        "cliente": cliente.pk,
        "data_inicio": inicio.isoformat(),
        "valor_semanal": "650,00",
        "dia_vencimento": "",
        "km_entrega": "48900",
        "limite_km": "ilimitado",
    }


def test_nova_alocacao_com_falha_na_primeira_semana_mostra_aviso(usuario_logado, monkeypatch):
    from apps.alocacoes import views
    from apps.financeiro.services import FalhaEmAlguns

    def falha(**kwargs):
        raise FalhaEmAlguns([], ["alocação 1 (FLX1A23)"])

    monkeypatch.setattr(views, "gerar_cobrancas_semanais", falha)
    veiculo = Veiculo.objects.create(placa="FLX1A23", marca_modelo="Onix", km_atual=48_900)
    cliente = Cliente.objects.create(nome="Bruna", cpf_cnpj="529.982.247-25")
    resposta = usuario_logado.post(
        reverse("alocacoes:nova"), _dados_nova(veiculo, cliente, HOJE), follow=True
    )
    assert resposta.status_code == 200
    alocacao = Alocacao.objects.get()  # o carro saiu: a alocação fica registrada
    assert alocacao.cliente == cliente
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.ALOCADO
    textos = [str(m) for m in resposta.context["messages"]]
    assert any("cobrança da 1ª semana não foi lançada" in t for t in textos)
    assert any("alocado a Bruna" in t for t in textos)


def test_nova_alocacao_sem_falha_continua_igual(usuario_logado):
    veiculo = Veiculo.objects.create(placa="FLX2A23", marca_modelo="Onix", km_atual=48_900)
    cliente = Cliente.objects.create(nome="Bruna", cpf_cnpj="529.982.247-25")
    resposta = usuario_logado.post(
        reverse("alocacoes:nova"), _dados_nova(veiculo, cliente, HOJE), follow=True
    )
    textos = [str(m) for m in resposta.context["messages"]]
    assert not any("1ª semana" in t for t in textos)
    assert Alocacao.objects.get().cobrancas.filter(origem="aluguel").count() == 1


# --- sobra 6: números do /admin com o jeito brasileiro ----------------------------------


def test_admin_lista_de_alocacoes_e_trocas_mostra_reais(admin_client):
    alocacao, troca = _troca_com_ajuste("SUB6A66")
    html = admin_client.get(reverse("admin:alocacoes_alocacao_changelist")).content.decode()
    assert "R$ 650,00" in html
    html = admin_client.get(reverse("admin:alocacoes_trocatemporaria_changelist")).content.decode()
    assert "R$ 840,00" in html


# --- sobra 7: um só jeito de dizer que a alocação acabou --------------------------------


def test_mensagens_de_alocacao_encerrada_unificadas(usuario_logado):
    from django.core.exceptions import ValidationError

    alocacao = _alocacao("ENC7A77", "Rita", "529.982.247-25", HOJE - timedelta(days=2))
    velha = Alocacao.objects.get(pk=alocacao.pk)
    alocacao.encerrar(HOJE, 50_100)
    with pytest.raises(ValidationError) as erro:
        velha.encerrar(HOJE, 50_200)
    assert erro.value.messages == [ALOCACAO_JA_ENCERRADA]
    for nome in ("encerrar", "troca_nova", "editar"):
        resposta = usuario_logado.get(reverse(f"alocacoes:{nome}", args=[alocacao.pk]), follow=True)
        textos = [str(m) for m in resposta.context["messages"]]
        assert any(t.startswith(ALOCACAO_JA_ENCERRADA) for t in textos), (nome, textos)
        assert not any("locação já" in t.lower().replace("alocação", "") for t in textos)


# --- refatoração: editar alocação é tudo ou nada -----------------------------------------


def test_editar_com_falha_ao_refazer_a_semana_nao_grava_nada(usuario_logado, monkeypatch):
    import apps.financeiro.services as servicos

    alocacao = _alocacao("EDI1A11", "Rita", "529.982.247-25", HOJE - timedelta(days=2))
    financeiro.gerar_cobrancas_semanais(alocacao=alocacao)

    def cai(*args, **kwargs):
        raise RuntimeError("conexão caiu")

    monkeypatch.setattr(servicos, "refazer_alugueis_em_aberto", cai)
    usuario_logado.raise_request_exception = True
    with pytest.raises(RuntimeError):
        usuario_logado.post(
            reverse("alocacoes:editar", args=[alocacao.pk]),
            {
                "valor_semanal": "700,00",
                "dia_vencimento": alocacao.dia_vencimento,
                "limite_km": "ilimitado",
                "caucao_valor": "500,00",
            },
        )
    alocacao.refresh_from_db()
    assert alocacao.valor_semanal == Decimal("650.00")
    assert alocacao.caucao_valor is None
    assert not Caucao.objects.filter(alocacao=alocacao).exists()


def test_editar_valor_continua_refazendo_a_semana_e_a_caucao(usuario_logado):
    alocacao = _alocacao("EDI2A22", "Rita", "529.982.247-25", HOJE - timedelta(days=2))
    financeiro.gerar_cobrancas_semanais(alocacao=alocacao)
    resposta = usuario_logado.post(
        reverse("alocacoes:editar", args=[alocacao.pk]),
        {
            "valor_semanal": "700,00",
            "dia_vencimento": alocacao.dia_vencimento,
            "limite_km": "ilimitado",
            "caucao_valor": "500,00",
        },
        follow=True,
    )
    alocacao.refresh_from_db()
    assert alocacao.valor_semanal == Decimal("700.00")
    assert Caucao.objects.filter(alocacao=alocacao).exists()
    textos = [str(m) for m in resposta.context["messages"]]
    assert any("foi refeita: R$ 700,00" in t for t in textos)


# --- refatoração: encerrar/devolver gravam só o que mudaram -------------------------------


def test_encerrar_nao_apaga_o_que_outra_tela_gravou_no_meio(db):
    alocacao = _alocacao("UPD1A11", "Rita", "529.982.247-25", HOJE - timedelta(days=2))
    Alocacao.objects.filter(pk=alocacao.pk).update(
        observacoes="combinado pagar no pix", contrato_aberto_em=HOJE
    )
    alocacao.encerrar(HOJE, 50_100)  # instância carregada antes da outra tela gravar
    alocacao.refresh_from_db()
    assert alocacao.observacoes == "combinado pagar no pix"
    assert alocacao.contrato_aberto_em == HOJE
    assert (alocacao.status, alocacao.data_termino, alocacao.km_devolucao) == (
        "encerrada",
        HOJE,
        50_100,
    )
    ultima = alocacao.history.first()
    assert (ultima.status, ultima.km_devolucao) == ("encerrada", 50_100)


def test_devolver_nao_apaga_motivo_e_grava_o_km_da_retirada_corrigido(db):
    alocacao, troca = _troca_com_ajuste("SUB8A88")
    semana = alocacao.cobrancas.get(origem=Cobranca.Origem.ALUGUEL)
    TrocaTemporaria.objects.filter(pk=troca.pk).update(motivo="Oficina", observacoes="obs")
    troca.km_retirada = 29_000  # corrigido na tela de devolução (só em memória)
    troca.devolver(HOJE, 30_200)
    troca.refresh_from_db()
    assert (troca.motivo, troca.observacoes) == ("Oficina", "obs")
    assert (troca.km_retirada, troca.km_devolucao, troca.data_devolucao) == (29_000, 30_200, HOJE)
    # D02 continua: só hoje a R$ 840, os outros 6 dias a R$ 650 → 4.740 ÷ 7
    semana.refresh_from_db()
    assert semana.valor == Decimal("677.14")
    assert troca.history.first().km_retirada == 29_000


# --- revisão: a versão do histórico gravada pelo encerrar/devolver bate com o banco -----


def test_historico_do_encerrar_nao_volta_a_observacao_gravada_no_meio(db):
    alocacao = _alocacao("HIS1A11", "Rita", "529.982.247-25", HOJE - timedelta(days=2))
    Alocacao.objects.filter(pk=alocacao.pk).update(
        observacoes="combinado pagar no pix", contrato_aberto_em=HOJE
    )
    alocacao.encerrar(HOJE, 50_100)  # instância carregada antes da outra tela gravar
    ultima = alocacao.history.first()
    # sem isso o Histórico mostrava a observação "apagada" no encerramento
    assert (ultima.observacoes, ultima.contrato_aberto_em) == ("combinado pagar no pix", HOJE)
    assert (ultima.status, ultima.km_devolucao) == ("encerrada", 50_100)


def test_historico_do_devolver_nao_volta_o_motivo_gravado_no_meio(db):
    alocacao, troca = _troca_com_ajuste("SUB9A99")
    TrocaTemporaria.objects.filter(pk=troca.pk).update(motivo="Oficina", observacoes="obs")
    troca.km_retirada = 29_000  # corrigido na tela de devolução (só em memória)
    troca.devolver(HOJE, 30_200)
    ultima = troca.history.first()
    assert (ultima.motivo, ultima.observacoes) == ("Oficina", "obs")
    assert (ultima.km_retirada, ultima.km_devolucao) == (29_000, 30_200)


# --- revisão: editar (agora tudo ou nada) trava na mesma ordem do encerrar ---------------


def _dados_editar(alocacao, valor="700,00"):
    return {
        "valor_semanal": valor,
        "dia_vencimento": alocacao.dia_vencimento,
        "limite_km": "ilimitado",
        "caucao_valor": "",
    }


def test_editar_trava_o_cliente_antes_da_alocacao(usuario_logado, monkeypatch):
    # com a transação única, alocação → cliente aqui e cliente → alocação no
    # encerrar se esperavam para sempre no PostgreSQL (duas abas)
    alocacao = _alocacao("EDI3A33", "Rita", "529.982.247-25", HOJE - timedelta(days=2))
    financeiro.gerar_cobrancas_semanais(alocacao=alocacao)
    url = reverse("alocacoes:editar", args=[alocacao.pk])
    travas = _travas_durante(lambda: usuario_logado.post(url, _dados_editar(alocacao)), monkeypatch)
    assert travas[:2] == [Cliente, Alocacao]
    alocacao.refresh_from_db()
    assert alocacao.valor_semanal == Decimal("700.00")


def test_editar_nao_reabre_alocacao_encerrada_no_meio_do_envio(usuario_logado, monkeypatch):
    from apps.alocacoes import views

    alocacao = _alocacao("EDI4A44", "Rita", "529.982.247-25", HOJE - timedelta(days=2))
    valida = views.AlocacaoEdicaoForm.is_valid

    def outra_aba_encerra(self):
        Alocacao.objects.get(pk=alocacao.pk).encerrar(HOJE, 50_100)
        return valida(self)

    monkeypatch.setattr(views.AlocacaoEdicaoForm, "is_valid", outra_aba_encerra)
    resposta = usuario_logado.post(
        reverse("alocacoes:editar", args=[alocacao.pk]), _dados_editar(alocacao), follow=True
    )
    alocacao.refresh_from_db()
    assert (alocacao.status, alocacao.km_devolucao) == ("encerrada", 50_100)
    assert alocacao.valor_semanal == Decimal("650.00")
    textos = [str(m) for m in resposta.context["messages"]]
    assert f"{ALOCACAO_JA_ENCERRADA} Não dá mais para editar." in textos


# --- achados do navegador (regressão 18c) --------------------------------------------------


def _alocar(cliente_http, inicio, cobrar=None, valor="650,00", placa="NAV1A23"):
    veiculo = Veiculo.objects.create(placa=placa, marca_modelo="Onix", km_atual=48_900)
    cliente = Cliente.objects.create(nome="Paulo Navegador", cpf_cnpj="529.982.247-25")
    dados = _dados_nova(veiculo, cliente, inicio)
    dados["valor_semanal"] = valor
    if cobrar:
        dados["cobrar_a_partir_de"] = cobrar.isoformat()
    return cliente_http.post(reverse("alocacoes:nova"), dados, follow=True)


def test_nova_alocacao_ajuda_do_cobrar_a_partir_de_nao_contradiz_o_erro(usuario_logado):
    html = usuario_logado.get(reverse("alocacoes:nova")).content.decode()
    # o "Vazio = cobra desde a data de início" só aparece no caso em que é verdade
    opcional = re.search(r'<p[^>]*data-ajuda-cobrar="opcional"[^>]*>(.*?)</p>', html, re.S)
    obrigatorio = re.search(r'<p[^>]*data-ajuda-cobrar="obrigatorio"[^>]*>(.*?)</p>', html, re.S)
    assert 'x-show="!inicioAntigo"' in opcional.group(0)
    assert "Vazio = cobra desde a data de início" in opcional.group(1)
    assert 'x-show="inicioAntigo"' in obrigatorio.group(0)
    assert "Obrigatório quando o início tem mais de uma semana" in obrigatorio.group(1)
    assert "Vazio" not in obrigatorio.group(1)
    assert "Cobrar desde o início</button>" in html
    # e o envio com o campo vazio explica o mesmo caminho
    resposta = _alocar(usuario_logado, HOJE - timedelta(days=21))
    assert not Alocacao.objects.exists()
    erro = resposta.context["form"].errors["cobrar_a_partir_de"][0]
    assert "Cobrar desde o início" in erro


def test_alocar_com_inicio_antigo_nao_promete_passos_que_nao_aparecem(usuario_logado):
    inicio = HOJE - timedelta(days=21)
    resposta = _alocar(usuario_logado, inicio, cobrar=inicio)
    alocacao = Alocacao.objects.get()
    from apps.alocacoes.entrega import passos_da_entrega

    assert passos_da_entrega(alocacao) is None
    textos = [str(m) for m in resposta.context["messages"]]
    assert "Veículo NAV1A23 alocado a Paulo Navegador." in textos
    assert not any("próximos passos" in t for t in textos)


def test_entrega_lancada_com_atraso_cobrando_desde_o_inicio_mantem_os_passos(usuario_logado):
    from apps.alocacoes.entrega import passos_da_entrega

    inicio = HOJE - timedelta(days=10)
    resposta = _alocar(usuario_logado, inicio, cobrar=inicio)
    alocacao = Alocacao.objects.get()
    assert passos_da_entrega(alocacao) is not None
    textos = [str(m) for m in resposta.context["messages"]]
    assert any("Siga os próximos passos da entrega" in t for t in textos)
    # contrato antigo de verdade (cobra só a partir de uma data depois do início): sem cartão
    alocacao.cobrar_a_partir_de = inicio + timedelta(days=7)
    assert passos_da_entrega(alocacao) is None


def test_valor_semanal_com_casas_demais_tem_mensagem_simples(usuario_logado):
    resposta = _alocar(usuario_logado, HOJE, valor="650,00650")
    assert not Alocacao.objects.exists()
    erro = resposta.context["form"].errors["valor_semanal"][0]
    assert erro == "Confira o valor — use só 2 casas depois da vírgula (ex.: 650,00)."


def test_campos_de_dinheiro_preenchidos_selecionam_tudo_ao_tocar(usuario_logado):
    html = usuario_logado.get(reverse("alocacoes:nova")).content.decode()
    assert "@focusin=" in html and "valor_semanal" in html and ".select()" in html


def test_titulo_da_vistoria_com_troca_usa_a_placa_do_substituto(usuario_logado):
    alocacao, _troca = _troca_com_ajuste(placa_sub="SUB2B22")
    url = reverse("alocacoes:vistoria_nova", args=[alocacao.pk]) + "?tipo=saida"
    html = usuario_logado.get(url).content.decode()
    titulo = re.search(r"<title>(.*?)</title>", html, re.S).group(1)
    assert "SUB2B22 (substituto do PRI1A11)" in titulo
    h1 = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", re.search(r"<h1.*?</h1>", html, re.S).group(0)))
    assert "Vistoria — SUB2B22 (substituto do PRI1A11)" in h1


def test_titulo_da_vistoria_sem_troca_continua_com_o_carro_da_locacao(usuario_logado):
    alocacao = _alocacao("PRI3C33", "Rita", "529.982.247-25", HOJE - timedelta(days=2))
    html = usuario_logado.get(reverse("alocacoes:vistoria_nova", args=[alocacao.pk])).content
    titulo = re.search(r"<title>(.*?)</title>", html.decode(), re.S).group(1)
    assert "Vistoria — PRI3C33 —" in titulo
    assert "substituto" not in titulo
