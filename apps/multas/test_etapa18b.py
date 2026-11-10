"""Etapa 18b (multas): achados dos testes no navegador.

Carro parado durante a troca, AIT repetido com hífen/espaço, FICI com prazo
vencido, "Prazo perdido" marcado à mão, NIC já registrada, edição que troca o
cliente, órgão com autocomplete, histórico com os rótulos da tela, vendedor
anterior em "Marcar paga", total da ND e o item da ND com placa e AIT.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.frota.models import Veiculo
from apps.multas import services
from apps.multas.forms import MultaEdicaoForm, MultaForm, OrgaoForm
from apps.multas.models import Multa, OrgaoAutuador
from apps.pessoas.models import Cliente

HOJE = timezone.localdate()


@pytest.fixture
def diego(db):
    return Cliente.objects.create(nome="Diego", cpf_cnpj="111.222.333-44")


@pytest.fixture
def argo(diego):
    """Carro de Diego desde -60; na oficina com troca aberta desde -10."""
    veiculo = Veiculo.objects.create(placa="SHN2E34", marca_modelo="Argo")
    Alocacao.objects.create(
        veiculo=veiculo,
        cliente=diego,
        data_inicio=HOJE - timedelta(days=60),
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
    )
    return veiculo


@pytest.fixture
def substituto(argo):
    carro = Veiculo.objects.create(placa="RBC5F67", marca_modelo="Mobi")
    TrocaTemporaria.objects.create(
        alocacao=argo.alocacoes.get(),
        veiculo_substituto=carro,
        data_retirada=HOJE - timedelta(days=10),
        km_retirada=1000,
    )
    return carro


def _multa(veiculo, **campos):
    campos.setdefault("data_infracao", HOJE - timedelta(days=20))
    campos.setdefault("valor", Decimal("195.23"))
    campos.setdefault("descricao", "Avançar sinal")
    return Multa.objects.create(veiculo=veiculo, **campos)


def _dados(veiculo, dia, **extra):
    dados = {
        "veiculo": veiculo.pk,
        "data_infracao": dia.isoformat(),
        "descricao": "Avançar sinal",
        "valor": "195,23",
        "tipo_condutor": Multa.TipoCondutor.CLIENTE,
        "responsavel": Multa.Responsavel.CLIENTE,
    }
    dados.update(extra)
    return dados


def _dados_edicao(multa, **extra):
    dados = {
        "veiculo": multa.veiculo_id,
        "data_infracao": multa.data_infracao.isoformat(),
        "cliente": multa.cliente_id or "",
        "descricao": multa.descricao,
        "valor": str(multa.valor).replace(".", ",") if multa.valor is not None else "",
        "ait": multa.ait,
        "orgao": multa.orgao_id or "",
        "fici_prazo": multa.fici_prazo.isoformat() if multa.fici_prazo else "",
        "tipo_condutor": multa.tipo_condutor,
        "responsavel": multa.responsavel,
        "resultado": multa.resultado,
        "fici_status": multa.fici_status,
        "pagamento": multa.pagamento,
        "pago_por": multa.pago_por,
    }
    dados.update(extra)
    return dados


# --- carro parado na oficina durante a troca ---


def test_multa_do_carro_na_oficina_fica_sem_cliente(argo, substituto, diego):
    multa = _multa(argo, data_infracao=HOJE - timedelta(days=5))
    assert multa.cliente is None
    assert multa.repasse == "Sem cliente: definir responsável"


def test_multa_do_substituto_vai_para_o_cliente(argo, substituto, diego):
    assert _multa(substituto, data_infracao=HOJE - timedelta(days=5)).cliente == diego


def test_multa_no_dia_da_retirada_continua_com_o_cliente(argo, substituto, diego):
    # no dia da troca o cliente pode ter dirigido o próprio carro até a oficina
    assert _multa(argo, data_infracao=HOJE - timedelta(days=10)).cliente == diego


def test_quem_estava_avisa_que_o_carro_estava_na_oficina(usuario_logado, argo, substituto, diego):
    dia = (HOJE - timedelta(days=5)).isoformat()
    html = usuario_logado.get(
        "/multas/quem-estava/", {"veiculo": argo.pk, "data_infracao": dia}
    ).content.decode()
    assert "na oficina" in html and "RBC5F67" in html
    assert "estava com: <strong>Diego" not in html


# --- AIT repetido ---


@pytest.mark.parametrize("ait", ["LEIGO-2", "leigo 2", "LEIGO.2"])
def test_ait_com_hifen_ou_espaco_e_o_mesmo(argo, diego, ait):
    orgao = OrgaoAutuador.objects.create(nome="BHTrans")
    _multa(argo, ait="LEIGO2", orgao=orgao)
    form = MultaForm(_dados(argo, HOJE - timedelta(days=20), ait=ait, orgao=orgao.pk))
    assert not form.is_valid()
    assert "já foi cadastrada" in str(form.errors["ait"])


def test_ait_repetido_com_orgao_em_branco(argo, diego):
    orgao = OrgaoAutuador.objects.create(nome="BHTrans")
    _multa(argo, ait="LEIGO2", orgao=orgao)
    form = MultaForm(_dados(argo, HOJE - timedelta(days=20), ait="LEIGO2"))
    assert not form.is_valid()
    assert "ait" in form.errors


def test_ait_e_gravado_so_com_letras_e_numeros(argo, diego):
    form = MultaForm(_dados(argo, HOJE - timedelta(days=20), ait="der-444 555"))
    assert form.is_valid(), form.errors
    assert form.save().ait == "DER444555"


def test_mesmo_numero_em_outro_orgao_e_outro_carro_passa(argo, diego):
    outro = Veiculo.objects.create(placa="QXA1B23", marca_modelo="Gol")
    _multa(outro, ait="123456", orgao=OrgaoAutuador.objects.create(nome="DER"))
    prf = OrgaoAutuador.objects.create(nome="PRF")
    form = MultaForm(_dados(argo, HOJE - timedelta(days=20), ait="123456", orgao=prf.pk))
    assert form.is_valid(), form.errors


# --- FICI com prazo vencido ---


def test_indicar_com_prazo_vencido_e_recusado(usuario_logado, argo, diego):
    multa = _multa(argo, fici_prazo=HOJE - timedelta(days=9))
    resposta = usuario_logado.post(f"/multas/{multa.pk}/fici/", follow=True)
    multa.refresh_from_db()
    assert multa.fici_status == Multa.Fici.PENDENTE
    assert "O prazo para indicar venceu" in resposta.content.decode()


def test_indicar_dentro_do_prazo_continua_igual(usuario_logado, argo, diego):
    multa = _multa(argo, fici_prazo=HOJE + timedelta(days=3))
    usuario_logado.post(f"/multas/{multa.pk}/fici/")
    multa.refresh_from_db()
    assert multa.fici_status == Multa.Fici.INDICADO


def test_lista_com_prazo_vencido_so_oferece_nic(usuario_logado, argo, diego):
    multa = _multa(argo, fici_prazo=HOJE - timedelta(days=9))
    html = usuario_logado.get("/multas/").content.decode()
    assert f"/multas/{multa.pk}/nic/" in html
    assert f"/multas/{multa.pk}/fici/" not in html


# --- "Prazo perdido" marcado em Editar ---


def test_prazo_perdido_a_mao_ainda_registra_nic(usuario_logado, argo, diego):
    multa = _multa(argo, fici_prazo=HOJE - timedelta(days=9), fici_status=Multa.Fici.PRAZO_PERDIDO)
    assert f"/multas/{multa.pk}/nic/" in usuario_logado.get("/multas/").content.decode()
    usuario_logado.post(f"/multas/{multa.pk}/nic/", {"valor": "300,00"})
    nic = Multa.objects.get(multa_origem_nic=multa)
    assert nic.valor == Decimal("300.00")
    # depois da NIC o botão some
    assert f"/multas/{multa.pk}/nic/" not in usuario_logado.get("/multas/").content.decode()


# --- NIC registrada trava a indicação ---


@pytest.mark.parametrize("volta_para", [Multa.Fici.INDICADO, Multa.Fici.PENDENTE])
def test_com_nic_a_original_nao_volta_a_indicado(argo, diego, volta_para):
    multa = _multa(argo, fici_prazo=HOJE - timedelta(days=9))
    services.registrar_nic(multa, valor=Decimal("300.00"))
    multa.refresh_from_db()
    form = MultaEdicaoForm(_dados_edicao(multa, fici_status=volta_para), instance=multa)
    assert not form.is_valid()
    assert "já tem NIC registrada" in str(form.errors["fici_status"])


def test_fici_da_nic_fica_travado(argo, diego):
    multa = _multa(argo, fici_prazo=HOJE - timedelta(days=9))
    nic = services.registrar_nic(multa, valor=Decimal("300.00"))
    form = MultaEdicaoForm(
        _dados_edicao(nic, fici_status=Multa.Fici.PENDENTE, valor="300,00"), instance=nic
    )
    assert form.is_valid(), form.errors
    assert form.save().fici_status == Multa.Fici.NAO_SE_APLICA


# --- Editar: trocar a data muda o cliente ---


def test_editar_data_avisa_o_novo_cliente(usuario_logado, argo, diego):
    multa = _multa(argo)
    resposta = usuario_logado.post(
        f"/multas/{multa.pk}/editar/",
        _dados_edicao(multa, data_infracao=(HOJE - timedelta(days=90)).isoformat()),
        follow=True,
    )
    multa.refresh_from_db()
    assert multa.cliente is None
    assert "agora atribuída a: nenhum cliente na data" in resposta.content.decode()


def test_quem_estava_na_edicao_limpa_o_cliente_salvo(usuario_logado, argo, diego):
    multa = _multa(argo)
    dia = (HOJE - timedelta(days=90)).isoformat()
    html = usuario_logado.get(
        "/multas/quem-estava/",
        {"veiculo": argo.pk, "data_infracao": dia, "cliente": diego.pk, "multa": multa.pk},
    ).content.decode()
    assert "a multa fica sem cliente" in html
    assert 'id="id_cliente" hx-swap-oob="innerHTML"' in html
    # a pessoa escolheu o cliente no próprio campo (o onchange marca o campo
    # escondido; o HX-Trigger do htmx é sempre a caixa do aviso): não limpa
    html = usuario_logado.get(
        "/multas/quem-estava/",
        {
            "veiculo": argo.pk,
            "data_infracao": dia,
            "cliente": diego.pk,
            "multa": multa.pk,
            "cliente_escolhido": "1",
        },
        HTTP_HX_TRIGGER="quem-estava",
    ).content.decode()
    assert 'hx-swap-oob="innerHTML"' not in html


def test_edicao_manda_o_id_da_multa_para_o_aviso(usuario_logado, argo, diego):
    multa = _multa(argo)
    html = usuario_logado.get(f"/multas/{multa.pk}/editar/").content.decode()
    assert f'hx-vals=\'{{"multa": "{multa.pk}"}}\'' in html
    assert "<title>Editar multa — SHN2E34" in html


# --- anular a original avisa da NIC ---


def test_anular_original_avisa_da_nic(usuario_logado, argo, diego):
    multa = _multa(argo, fici_prazo=HOJE - timedelta(days=9))
    services.registrar_nic(multa, valor=Decimal("300.00"))
    multa.refresh_from_db()
    resposta = usuario_logado.post(
        f"/multas/{multa.pk}/editar/",
        _dados_edicao(multa, resultado=Multa.Resultado.CANCELADA),
        follow=True,
    )
    assert "Esta multa tinha NIC" in resposta.content.decode()


# --- "Condutor identificado" sai do formulário ---


def test_condutor_identificado_nao_aparece_na_multa_nova(db):
    opcoes = [valor for valor, _ in MultaForm().fields["responsavel"].choices]
    assert Multa.Responsavel.CONDUTOR not in opcoes


def test_condutor_identificado_antigo_continua_na_edicao(argo, diego):
    multa = _multa(argo, responsavel=Multa.Responsavel.CONDUTOR)
    opcoes = [valor for valor, _ in MultaEdicaoForm(instance=multa).fields["responsavel"].choices]
    assert Multa.Responsavel.CONDUTOR in opcoes


# --- órgão: o navegador não preenche login/senha do sistema ---


def test_orgao_sem_autocompletar_credenciais(db):
    html = str(OrgaoForm())
    assert 'autocomplete="new-password"' in html
    assert 'autocomplete="off"' in html


# --- histórico com os rótulos da tela ---


def test_historico_usa_os_rotulos_do_formulario(argo, diego):
    multa = _multa(argo)
    multa.fici_status = Multa.Fici.PRAZO_PERDIDO
    multa.save()
    campos = [m["campo"] for evento in services.historico(multa) for m in evento["mudancas"]]
    assert "Indicação do motorista (FICI)" in campos
    assert "Fici" not in campos


# --- vendedor anterior em "Marcar paga" ---


def test_marcar_paga_pelo_vendedor_anterior(usuario_logado, argo, diego):
    multa = _multa(argo, responsavel=Multa.Responsavel.VENDEDOR)
    assert 'value="Vendedor anterior"' in usuario_logado.get("/multas/").content.decode()
    usuario_logado.post(f"/multas/{multa.pk}/paga/", {"pago_por": "Vendedor anterior"})
    multa.refresh_from_db()
    assert multa.pago_por == Multa.PAGO_POR_VENDEDOR


def test_cliente_nao_pode_marcar_vendedor(usuario_logado, argo, diego):
    multa = _multa(argo)
    usuario_logado.post(f"/multas/{multa.pk}/paga/", {"pago_por": "Vendedor anterior"})
    multa.refresh_from_db()
    assert multa.pago_por == ""


# --- Gerar ND: total antes de emitir e item com placa e AIT ---


def test_gerar_nd_mostra_o_total(usuario_logado, argo, diego):
    _multa(argo)
    _multa(argo, valor=Decimal("100.00"), descricao="Velocidade")
    html = usuario_logado.get(f"/multas/gerar-nd/?cliente={diego.pk}").content.decode()
    assert "Total marcado" in html and "R$ 295,23" in html
    assert 'data-valor="195.23"' in html


def test_item_da_nd_tem_ait_placa_e_codigo(argo, diego):
    multa = _multa(argo, ait="DER4445556", codigo="7366-2", descricao="Usar o celular")
    nd = services.gerar_nd_de_multas(diego, [multa], HOJE)
    descricao = nd.itens.get().descricao
    assert descricao == (
        f"Multa AIT DER4445556 — SHN2E34 — Usar o celular "
        f"({multa.data_infracao:%d/%m/%Y}) · cód. 7366-2"
    )


# --- revisão: cliente escolhido de novo à mão depois de trocar a data ---


def test_edicao_tem_o_campo_que_marca_o_cliente_escolhido(usuario_logado, argo, diego):
    """O HX-Trigger do htmx é o id da caixa do aviso, nunca o do select do cliente:
    quem diz que a pessoa escolheu é o campo escondido, marcado pelo onchange."""
    multa = _multa(argo)
    html = usuario_logado.get(f"/multas/{multa.pk}/editar/").content.decode()
    assert 'id="cliente_escolhido"' in html
    assert "#cliente_escolhido" in html  # vai junto no pedido do aviso
    assert "getElementById(&#x27;cliente_escolhido&#x27;).value = &#x27;1&#x27;" in html
    # cadastro não tem o campo (o aviso nunca limpa o cliente lá)
    assert 'id="cliente_escolhido"' not in usuario_logado.get("/multas/nova/").content.decode()


def test_cliente_escolhido_de_novo_a_mao_fica_ao_salvar(usuario_logado, argo, diego):
    multa = _multa(argo)
    usuario_logado.post(
        f"/multas/{multa.pk}/editar/",
        _dados_edicao(
            multa,
            data_infracao=(HOJE - timedelta(days=90)).isoformat(),
            cliente=diego.pk,
            cliente_escolhido="1",
        ),
    )
    multa.refresh_from_db()
    assert multa.data_infracao == HOJE - timedelta(days=90)
    assert multa.cliente == diego  # a escolha dela vale; não recalcula pela data


def test_sem_a_marca_o_cliente_salvo_e_recalculado(argo, diego):
    multa = _multa(argo)
    form = MultaEdicaoForm(
        _dados_edicao(multa, data_infracao=(HOJE - timedelta(days=90)).isoformat()),
        instance=multa,
    )
    assert form.is_valid(), form.errors
    assert form.save().cliente is None


def test_marca_sem_cliente_nao_impede_o_recalculo(argo, diego):
    multa = _multa(argo)
    form = MultaEdicaoForm(
        _dados_edicao(
            multa,
            data_infracao=(HOJE - timedelta(days=90)).isoformat(),
            cliente="",
            cliente_escolhido="1",
        ),
        instance=multa,
    )
    assert form.is_valid(), form.errors
    assert form.save().cliente is None


def test_erro_no_formulario_mantem_a_marca(usuario_logado, argo, diego):
    multa = _multa(argo)
    html = usuario_logado.post(
        f"/multas/{multa.pk}/editar/",
        _dados_edicao(multa, valor="-5", cliente_escolhido="1"),
    ).content.decode()
    assert 'id="cliente_escolhido" value="1"' in html


# --- revisão: lista sem consulta por linha com "Prazo perdido" ---


def test_lista_com_prazo_perdido_nao_consulta_por_linha(usuario_logado, argo, diego):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    def consultas():
        with CaptureQueriesContext(connection) as ctx:
            usuario_logado.get("/multas/")
        return len(ctx.captured_queries)

    for _ in range(2):
        _multa(argo, fici_status=Multa.Fici.PRAZO_PERDIDO, fici_prazo=HOJE - timedelta(days=9))
    poucas = consultas()
    for _ in range(4):
        _multa(argo, fici_status=Multa.Fici.PRAZO_PERDIDO, fici_prazo=HOJE - timedelta(days=9))
    assert consultas() == poucas


# --- revisão: AIT normalizado igual à busca global ---


def test_ait_normaliza_como_a_busca(db):
    from apps.busca import so_letras_e_digitos
    from apps.multas.forms import normalizar_ait

    for texto in ("R-123.456 / 2025", "  ab 12-c ", "", None, "São-1"):
        assert normalizar_ait(texto) == so_letras_e_digitos(texto)
