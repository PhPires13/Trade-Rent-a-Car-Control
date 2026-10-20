"""UX de multas: editar com histórico, aviso de quem estava com o carro, atalhos
de tarefa na lista, NIC só depois do prazo e cartões no celular (auditoria UX
#10, #11 e #13)."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.alocacoes.models import Alocacao
from apps.frota.models import Veiculo
from apps.multas import services
from apps.multas.models import Multa
from apps.pessoas.models import Cliente, CondutorAutorizado

HOJE = date.today()


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


# --- atalhos de tarefa (#11) ---


def test_atalhos_contam_e_filtram_as_tarefas(usuario_logado, veiculo, cliente):
    vencida = _multa(veiculo, descricao="Vencida", fici_prazo=HOJE - timedelta(days=2))
    proxima = _multa(veiculo, descricao="Próxima", fici_prazo=HOJE + timedelta(days=5))
    so_pagar = _multa(veiculo, descricao="Só pagar", fici_status=Multa.Fici.INDICADO)
    services.gerar_nd_de_multas(cliente, [so_pagar], HOJE)
    concluida = _multa(
        veiculo,
        descricao="Concluída",
        fici_status=Multa.Fici.INDICADO,
        pagamento=Multa.Pagamento.PAGO,
        responsavel=Multa.Responsavel.EMPRESA,
    )
    advertencia = _multa(veiculo, descricao="Advertência", resultado=Multa.Resultado.ADVERTENCIA)

    resposta = usuario_logado.get("/multas/")
    contas = {a["chave"]: a["total"] for a in resposta.context["atalhos"]}
    assert contas == {"indicar": 2, "pagar": 3, "cobrar": 2, "concluidas": 2}
    html = resposta.content.decode()
    assert "1 com prazo vencido" in html

    indicar = usuario_logado.get("/multas/", {"tarefa": "indicar"}).context["multas"]
    assert list(indicar) == [vencida, proxima]  # prazo mais próximo primeiro
    cobrar = usuario_logado.get("/multas/", {"tarefa": "cobrar"}).context["multas"]
    assert set(cobrar) == {vencida, proxima}  # a que já está em ND sai
    concluidas = usuario_logado.get("/multas/", {"tarefa": "concluidas"}).context["multas"]
    assert set(concluidas) == {concluida, advertencia}
    # atalho desconhecido não quebra: mostra todas
    assert len(usuario_logado.get("/multas/", {"tarefa": "xyz"}).context["multas"]) == 5


def test_filtro_por_cliente_e_link_da_ficha(usuario_logado, veiculo, cliente):
    _multa(veiculo, descricao="Do Arlen")
    outro = Veiculo.objects.create(placa="RNB9J66", marca_modelo="Voyage")
    _multa(outro, descricao="Sem cliente")
    resposta = usuario_logado.get("/multas/", {"cliente": cliente.pk})
    assert [m.descricao for m in resposta.context["multas"]] == ["Do Arlen"]
    assert "Cliente: <strong>Arlen</strong>" in resposta.content.decode()
    assert usuario_logado.get("/multas/", {"cliente": "abc"}).status_code == 200
    ficha = usuario_logado.get(f"/clientes/{cliente.pk}/").content.decode()
    assert f"/multas/?cliente={cliente.pk}" in ficha


def test_painel_aponta_para_indicar_condutor(usuario_logado, veiculo):
    _multa(veiculo, descricao="Prazo", fici_prazo=HOJE + timedelta(days=1))
    assert "/multas/?tarefa=indicar" in usuario_logado.get("/").content.decode()


# --- editar com histórico (#10) ---


def test_editar_muda_situacao_volta_aos_filtros_e_guarda_historico(usuario_logado, veiculo):
    multa = _multa(veiculo, descricao="Avançar sinal")
    resposta = usuario_logado.get(f"/multas/{multa.pk}/editar/?volta=tarefa%3Dpagar")
    assert resposta.status_code == 200
    html = resposta.content.decode()
    assert "Situação da multa" in html and "Histórico desta multa" in html
    assert "Multa registrada." in html

    resposta = usuario_logado.post(
        f"/multas/{multa.pk}/editar/",
        _dados_form(
            multa,
            volta="tarefa=pagar",
            resultado=Multa.Resultado.SUSPENSA,
            pagamento=Multa.Pagamento.PAGO,
            pago_por="Cliente",
        ),
    )
    assert resposta.status_code == 302
    assert resposta["Location"] == "/multas/?tarefa=pagar"
    multa.refresh_from_db()
    assert multa.resultado == Multa.Resultado.SUSPENSA
    assert multa.pago_por == "Cliente"

    html = usuario_logado.get(f"/multas/{multa.pk}/editar/").content.decode()
    assert "Em aberto → Suspensa" in html
    assert "Pendente → Pago" in html


def test_editar_nao_anula_multa_que_esta_em_nd(usuario_logado, veiculo, cliente):
    multa = _multa(veiculo, descricao="Cobrada")
    nd = services.gerar_nd_de_multas(cliente, [multa], HOJE)
    resposta = usuario_logado.post(
        f"/multas/{multa.pk}/editar/",
        _dados_form(multa, resultado=Multa.Resultado.ADVERTENCIA),
    )
    assert resposta.status_code == 200
    assert f"ND {nd.numero:03d}" in resposta.content.decode()
    multa.refresh_from_db()
    assert multa.resultado == Multa.Resultado.EM_ABERTO


def test_volta_nao_redireciona_para_fora(usuario_logado, veiculo):
    multa = _multa(veiculo)
    resposta = usuario_logado.post(f"/multas/{multa.pk}/fici/", {"volta": "//site-ruim.com/x?a=1"})
    assert resposta["Location"].startswith("/multas/?")


def test_condutor_some_quando_nao_e_outra_pessoa(usuario_logado, veiculo, cliente):
    condutor = CondutorAutorizado.objects.create(nome="Irmão", cliente=cliente)
    multa = _multa(veiculo)
    usuario_logado.post(
        f"/multas/{multa.pk}/editar/",
        _dados_form(multa, tipo_condutor="cliente", condutor_autorizado=condutor.pk),
    )
    multa.refresh_from_db()
    assert multa.condutor_autorizado is None
    usuario_logado.post(
        f"/multas/{multa.pk}/editar/",
        _dados_form(multa, tipo_condutor="outro", condutor_autorizado=condutor.pk),
    )
    multa.refresh_from_db()
    assert multa.condutor_autorizado == condutor


def test_formulario_em_blocos_com_rotulos_simples(usuario_logado, db):
    html = usuario_logado.get("/multas/nova/").content.decode()
    assert "1. O que veio na notificação" in html
    assert "2. Prazo para indicar o motorista" in html
    assert "3. Quem paga" in html
    assert "Número do auto de infração (AIT)" in html
    assert "Data limite para identificação do condutor" in html
    assert 'hx-get="/multas/quem-estava/"' in html


# --- aviso ao vivo "o carro estava com" (#10 B) ---


def test_quem_estava_mostra_o_cliente_e_filtra_condutores(usuario_logado, veiculo, cliente):
    do_cliente = CondutorAutorizado.objects.create(nome="Irmão do Arlen", cliente=cliente)
    outro_cliente = Cliente.objects.create(nome="Bia", cpf_cnpj="999.888.777-66")
    CondutorAutorizado.objects.create(nome="Marido da Bia", cliente=outro_cliente)
    avulso = CondutorAutorizado.objects.create(nome="Avulso")

    resposta = usuario_logado.get(
        "/multas/quem-estava/",
        {"veiculo": veiculo.pk, "data_infracao": (HOJE - timedelta(days=3)).isoformat()},
    )
    html = resposta.content.decode()
    assert "o carro estava com: <strong>Arlen</strong>" in html
    opcoes = list(resposta.context["campo_condutor"].field.queryset)
    assert opcoes == [avulso, do_cliente]
    assert 'hx-swap-oob="true"' in html


def test_quem_estava_sem_aluguel_e_dados_incompletos(usuario_logado, veiculo):
    antes = (HOJE - timedelta(days=400)).isoformat()
    html = usuario_logado.get(
        "/multas/quem-estava/", {"veiculo": veiculo.pk, "data_infracao": antes}
    ).content.decode()
    assert "não estava alugado" in html
    html = usuario_logado.get("/multas/quem-estava/", {"data_infracao": "lixo"}).content.decode()
    assert "Escolha o carro e a data" in html


# --- NIC só depois do prazo (#13 A) e "Quem pagou?" ---


def test_nic_so_aparece_e_so_registra_depois_do_prazo(usuario_logado, veiculo):
    no_prazo = _multa(veiculo, descricao="No prazo", fici_prazo=HOJE)
    html = usuario_logado.get("/multas/").content.decode()
    assert f"/multas/{no_prazo.pk}/nic/" not in html

    usuario_logado.post(f"/multas/{no_prazo.pk}/nic/", {"valor": "195,30"})
    assert not Multa.objects.filter(multa_origem_nic=no_prazo).exists()

    vencida = _multa(veiculo, descricao="Vencida", fici_prazo=HOJE - timedelta(days=1))
    html = usuario_logado.get("/multas/").content.decode()
    assert f"/multas/{vencida.pk}/nic/" in html
    assert "Valor da multa NIC (R$)" in html
    usuario_logado.post(f"/multas/{vencida.pk}/nic/", {"valor": "195,30"})
    assert vencida.multa_nic.valor == Decimal("195.30")


def test_marcar_paga_guarda_quem_pagou(usuario_logado, veiculo):
    multa = _multa(veiculo, fici_status=Multa.Fici.INDICADO)
    html = usuario_logado.get("/multas/").content.decode()
    assert 'name="pago_por" value="Empresa"' in html
    resposta = usuario_logado.post(
        f"/multas/{multa.pk}/paga/", {"pago_por": "Empresa", "volta": "tarefa=pagar"}, follow=True
    )
    assert resposta.redirect_chain[-1][0] == "/multas/?tarefa=pagar"
    multa.refresh_from_db()
    assert multa.pago_por == "Empresa"
    assert "Multa marcada como paga pela empresa." in resposta.content.decode()


# --- cartões no celular (#13) ---


def test_lista_tem_cartoes_no_celular_e_tabela_no_computador(usuario_logado, veiculo):
    multa = _multa(veiculo, descricao="Velocidade")
    html = usuario_logado.get("/multas/", {"tarefa": "indicar"}).content.decode()
    assert '<ul class="space-y-3 sm:hidden">' in html
    assert "hidden overflow-x-auto rounded-lg bg-white shadow sm:block" in html
    assert "Mais opções" in html
    assert f"/multas/{multa.pk}/editar/?volta=tarefa%3Dindicar" in html
