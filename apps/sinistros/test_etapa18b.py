"""Etapa 18b (sinistros): achados dos testes no navegador.

Franquia cobrada que precisa ser corrigida, sinistro cadastrado duas vezes,
culpa de terceiro com o motorista pagando, carro parado durante a troca,
auxílio com data antes do pedido (e a correção do recebido), roubo registrado
depois de encerrar a locação, os selos da lista e o título da aba.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.financeiro.models import AplicacaoRecebimento, Cobranca, Recebimento
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente
from apps.sinistros import services
from apps.sinistros.models import AuxilioMotorista, Sinistro
from apps.sinistros.views import SinistroForm

HOJE = timezone.localdate()


@pytest.fixture
def carla(db):
    return Cliente.objects.create(nome="Carla", cpf_cnpj="111.222.333-44")


@pytest.fixture
def veiculo(carla):
    veiculo = Veiculo.objects.create(placa="PUL7D89", marca_modelo="Onix", km_atual=1000)
    Alocacao.objects.create(
        veiculo=veiculo,
        cliente=carla,
        data_inicio=HOJE - timedelta(days=60),
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
    )
    return veiculo


def _sinistro(veiculo, **campos):
    campos.setdefault("data", HOJE - timedelta(days=16))
    campos.setdefault("envolvido", Sinistro.Envolvido.ASSOCIADO)
    return Sinistro.objects.create(veiculo=veiculo, **campos)


def _dados(veiculo, **extra):
    dados = {
        "veiculo": veiculo.pk,
        "data": (HOJE - timedelta(days=16)).isoformat(),
        "cliente": "",
        "tipo": Sinistro.Tipo.COLISAO,
        "envolvido": Sinistro.Envolvido.ASSOCIADO,
        "responsabilidade": Sinistro.Responsabilidade.INDEFINIDA,
        "descricao": "batida",
        "boletim_ocorrencia": "",
        "responsavel_custo": "empresa",
    }
    dados.update(extra)
    return dados


def _dados_edicao(sinistro, **extra):
    dados = _dados(
        sinistro.veiculo,
        data=sinistro.data.isoformat(),
        cliente=sinistro.cliente_id or "",
        tipo=sinistro.tipo,
        responsabilidade=sinistro.responsabilidade,
        boletim_ocorrencia=sinistro.boletim_ocorrencia,
        responsavel_custo=sinistro.responsavel_custo,
        status=sinistro.status,
        observacoes="",
    )
    if sinistro.acionou_protecao:
        dados["acionou_protecao"] = "on"
        dados["data_evento"] = sinistro.data_evento.isoformat()
        dados["franquia_valor"] = str(sinistro.franquia_valor).replace(".", ",")
    dados.update(extra)
    return dados


@pytest.fixture
def cobrado(veiculo):
    sinistro = _sinistro(
        veiculo,
        acionou_protecao=True,
        data_evento=HOJE - timedelta(days=15),
        franquia_valor=Decimal("1500.00"),
        responsavel_custo="cliente",
        responsabilidade=Sinistro.Responsabilidade.CLIENTE,
    )
    services.cobrar_franquia(sinistro)
    sinistro.refresh_from_db()
    return sinistro


# --- franquia cobrada: cancelar, corrigir e cobrar de novo ---


def test_cancelar_corrigir_e_cobrar_de_novo(usuario_logado, cobrado, carla):
    antiga = cobrado.cobranca_franquia
    html = usuario_logado.get(f"/sinistros/{cobrado.pk}/editar/").content.decode()
    assert "Cancelar esta cobrança" in html
    resposta = usuario_logado.post(f"/sinistros/{cobrado.pk}/cancelar-franquia/", follow=True)
    assert "cancelada" in resposta.content.decode()
    antiga.refresh_from_db()
    assert antiga.status == Cobranca.Status.CANCELADA
    cobrado.refresh_from_db()
    form = SinistroForm(_dados_edicao(cobrado, franquia_valor="150,00"), instance=cobrado)
    assert form.is_valid(), form.errors
    form.save()
    nova = services.cobrar_franquia(Sinistro.objects.get(pk=cobrado.pk))
    assert nova.pk != antiga.pk and nova.valor == Decimal("150.00")


def test_franquia_com_pagamento_nao_cancela(cobrado, carla):
    cobranca = cobrado.cobranca_franquia
    recebimento = Recebimento.objects.create(
        cliente=carla, data=HOJE, valor=Decimal("100.00"), forma=Recebimento.Forma.PIX
    )
    AplicacaoRecebimento.objects.create(
        recebimento=recebimento, cobranca=cobranca, valor=Decimal("100.00")
    )
    with pytest.raises(ValidationError, match="pagamento aplicado"):
        services.cancelar_cobranca_franquia(cobrado)
    cobranca.refresh_from_db()
    assert cobranca.status != Cobranca.Status.CANCELADA


def test_franquia_judicial_nao_cancela(cobrado):
    cobranca = cobrado.cobranca_franquia
    cobranca.status = Cobranca.Status.JUDICIAL
    cobranca.save(update_fields=["status"])
    with pytest.raises(ValidationError, match="judicial"):
        services.cancelar_cobranca_franquia(cobrado)


def test_aviso_da_trava_aponta_o_botao(cobrado):
    form = SinistroForm(_dados_edicao(cobrado, franquia_valor="150,00"), instance=cobrado)
    assert not form.is_valid()
    assert "Cancelar esta cobrança" in str(form.errors["franquia_valor"])


# --- sinistro cadastrado duas vezes ---


def test_mesmo_bo_e_recusado(veiculo):
    _sinistro(veiculo, boletim_ocorrencia="2026-123")
    form = SinistroForm(_dados(veiculo, boletim_ocorrencia="BO 2026 123"))
    assert not form.is_valid()
    assert "já foi cadastrado" in str(form.non_field_errors())
    assert "/editar/" in str(form.non_field_errors())


def test_mesmo_dia_e_tipo_sem_bo_e_recusado(veiculo):
    _sinistro(veiculo)
    form = SinistroForm(_dados(veiculo))
    assert not form.is_valid()


def test_outro_ocorrido_no_mesmo_dia_com_outro_bo_passa(veiculo):
    _sinistro(veiculo, boletim_ocorrencia="111")
    form = SinistroForm(_dados(veiculo, boletim_ocorrencia="222"))
    assert form.is_valid(), form.errors


def test_editar_nao_acusa_repetido(veiculo):
    sinistro = _sinistro(veiculo)
    form = SinistroForm(_dados_edicao(sinistro, descricao="outra"), instance=sinistro)
    assert form.is_valid(), form.errors


def test_reenvio_do_mesmo_formulario_mostra_o_aviso_de_envio(usuario_logado, veiculo):
    dados = _dados(veiculo, token="codigo-unico")
    usuario_logado.post("/sinistros/novo/", dados)
    resposta = usuario_logado.post("/sinistros/novo/", dados, follow=True)
    assert Sinistro.objects.count() == 1
    assert "envio repetido foi ignorado" in resposta.content.decode()


def test_outro_formulario_igual_e_recusado_na_tela(usuario_logado, veiculo):
    usuario_logado.post("/sinistros/novo/", _dados(veiculo, token="um"))
    resposta = usuario_logado.post("/sinistros/novo/", _dados(veiculo, token="dois"))
    assert Sinistro.objects.count() == 1
    assert "já foi cadastrado" in resposta.content.decode()


# --- culpa de terceiro, motorista pagando ---


def test_culpa_de_terceiro_com_cliente_pagando_e_recusado(veiculo):
    form = SinistroForm(
        _dados(
            veiculo,
            responsabilidade=Sinistro.Responsabilidade.TERCEIRO,
            responsavel_custo="cliente",
        )
    )
    assert not form.is_valid()
    assert "outra pessoa" in str(form.errors["responsavel_custo"])


def test_formulario_novo_sugere_quem_paga_pela_culpa(usuario_logado, veiculo):
    html = usuario_logado.get("/sinistros/novo/").content.decode()
    assert "mexeuCusto" in html and 'x-model="custo"' in html


# --- carro parado durante a troca ---


def test_sinistro_do_carro_na_oficina_fica_sem_motorista(usuario_logado, veiculo):
    substituto = Veiculo.objects.create(placa="RBC5F67", marca_modelo="Mobi")
    TrocaTemporaria.objects.create(
        alocacao=veiculo.alocacoes.get(),
        veiculo_substituto=substituto,
        data_retirada=HOJE - timedelta(days=10),
        km_retirada=1000,
    )
    dia = HOJE - timedelta(days=5)
    assert _sinistro(veiculo, data=dia).cliente is None
    assert _sinistro(substituto, data=dia).cliente.nome == "Carla"
    html = usuario_logado.get(
        "/sinistros/quem-estava/", {"veiculo": veiculo.pk, "data": dia.isoformat()}
    ).content.decode()
    assert "estava na oficina" in html and "RBC5F67" in html


# --- auxílio: data antes do pedido e correção do recebido ---


@pytest.fixture
def auxilio(veiculo):
    sinistro = _sinistro(veiculo, acionou_protecao=True, data_evento=HOJE - timedelta(days=15))
    return AuxilioMotorista.objects.create(
        sinistro=sinistro,
        valor=Decimal("1518.00"),
        status=AuxilioMotorista.Status.SOLICITADO,
        data_solicitacao=HOJE - timedelta(days=2),
    )


def test_recebido_antes_do_pedido_e_recusado(usuario_logado, auxilio):
    resposta = usuario_logado.post(
        f"/sinistros/auxilio/{auxilio.pk}/receber/",
        {"data": (HOJE - timedelta(days=20)).isoformat()},
        follow=True,
    )
    auxilio.refresh_from_db()
    assert auxilio.status == AuxilioMotorista.Status.SOLICITADO
    assert "antes do pedido do auxílio" in resposta.content.decode()


def test_campo_de_data_tem_minimo(usuario_logado, auxilio):
    minimo = f'min="{auxilio.data_solicitacao:%Y-%m-%d}"'
    assert minimo in usuario_logado.get("/sinistros/").content.decode()
    editar = usuario_logado.get(f"/sinistros/{auxilio.sinistro_id}/editar/").content.decode()
    assert minimo in editar


def test_corrigir_auxilio_recebido(usuario_logado, auxilio):
    usuario_logado.post(f"/sinistros/auxilio/{auxilio.pk}/receber/", {"valor": "15.180,00"})
    html = usuario_logado.get(f"/sinistros/{auxilio.sinistro_id}/editar/").content.decode()
    assert "Salvar correção" in html
    usuario_logado.post(
        f"/sinistros/auxilio/{auxilio.pk}/corrigir/",
        {"valor": "1.518,00", "data": (HOJE - timedelta(days=1)).isoformat()},
    )
    auxilio.refresh_from_db()
    assert auxilio.valor == Decimal("1518.00")
    assert auxilio.data_recebimento == HOJE - timedelta(days=1)


def test_corrigir_auxilio_com_data_antes_do_pedido_e_recusado(usuario_logado, auxilio):
    usuario_logado.post(f"/sinistros/auxilio/{auxilio.pk}/receber/")
    usuario_logado.post(
        f"/sinistros/auxilio/{auxilio.pk}/corrigir/",
        {"valor": "1.518,00", "data": (HOJE - timedelta(days=30)).isoformat()},
    )
    auxilio.refresh_from_db()
    assert auxilio.data_recebimento == HOJE


# --- roubo registrado depois de encerrar a locação ---


def test_roubo_depois_da_locacao_encerrada_avisa(usuario_logado, veiculo, carla):
    alocacao = veiculo.alocacoes.get()
    alocacao.encerrar(HOJE, 1000)
    dados = _dados(
        veiculo,
        tipo=Sinistro.Tipo.ROUBO,
        data=(HOJE - timedelta(days=2)).isoformat(),
        token="roubo",
    )
    resposta = usuario_logado.post("/sinistros/novo/", dados, follow=True)
    html = resposta.content.decode()
    assert "foi encerrada em" in html and "depois do roubo" in html


# --- lista: franquia cobrada e situação do roubo ---


def test_lista_mostra_franquia_cobrada(usuario_logado, cobrado):
    html = usuario_logado.get("/sinistros/").content.decode()
    assert "Franquia cobrada de Carla" in html


def test_lista_mostra_carro_roubado_e_recuperado(usuario_logado, veiculo):
    roubo = _sinistro(veiculo, tipo=Sinistro.Tipo.ROUBO, data=HOJE - timedelta(days=3))
    services.registrar_roubo(roubo)
    html = usuario_logado.get("/sinistros/").content.decode()
    assert "Carro roubado — aguardando" in html and "Carro voltou? Registrar" in html
    services.carro_recuperado(roubo, HOJE - timedelta(days=1))
    html = usuario_logado.get("/sinistros/").content.decode()
    assert f"Recuperado em {HOJE - timedelta(days=1):%d/%m/%Y}" in html


def test_titulo_da_aba_de_editar(usuario_logado, veiculo):
    sinistro = _sinistro(veiculo)
    html = usuario_logado.get(f"/sinistros/{sinistro.pk}/editar/").content.decode()
    assert "<title>Editar sinistro — PUL7D89 — Trade Rent a Car</title>" in html


# --- revisão: cancelar só o que o botão pode cancelar ---


@pytest.mark.parametrize(
    "status", [Cobranca.Status.PARCIAL, Cobranca.Status.PAGO, Cobranca.Status.JUDICIAL]
)
def test_sem_botao_de_cancelar_com_pagamento_ou_judicial(usuario_logado, cobrado, status):
    cobranca = cobrado.cobranca_franquia
    cobranca.status = status
    cobranca.save(update_fields=["status"])
    html = usuario_logado.get(f"/sinistros/{cobrado.pk}/editar/").content.decode()
    assert "Cancelar esta cobrança</button>" not in html
    # e a trava do formulário não manda procurar um botão que não está lá
    cobrado.refresh_from_db()
    form = SinistroForm(_dados_edicao(cobrado, franquia_valor="150,00"), instance=cobrado)
    assert not form.is_valid()
    erro = str(form.errors["franquia_valor"])
    assert "Cancelar esta cobrança" not in erro
    assert "desfaça o pagamento no Financeiro" in erro


def test_botao_de_cancelar_com_cobranca_atrasada(usuario_logado, cobrado):
    cobranca = cobrado.cobranca_franquia
    cobranca.status = Cobranca.Status.ATRASADO
    cobranca.save(update_fields=["status"])
    html = usuario_logado.get(f"/sinistros/{cobrado.pk}/editar/").content.decode()
    assert "Cancelar esta cobrança</button>" in html
    usuario_logado.post(f"/sinistros/{cobrado.pk}/cancelar-franquia/")
    cobranca.refresh_from_db()
    assert cobranca.status == Cobranca.Status.CANCELADA


def test_cancelar_duas_vezes_nao_quebra(usuario_logado, cobrado):
    url = f"/sinistros/{cobrado.pk}/cancelar-franquia/"
    usuario_logado.post(url)
    resposta = usuario_logado.post(url, follow=True)
    assert resposta.status_code == 200
    assert "já estava cancelada" in resposta.content.decode()


def test_cancelar_por_get_nao_cancela(usuario_logado, cobrado):
    usuario_logado.get(f"/sinistros/{cobrado.pk}/cancelar-franquia/")
    cobrado.cobranca_franquia.refresh_from_db()
    assert cobrado.cobranca_franquia.status != Cobranca.Status.CANCELADA


def test_cancelar_volta_o_cliente_a_ativo(cobrado, carla):
    """Franquia atrasada era a única dívida: cancelar tira a inadimplência na hora."""
    from apps.financeiro.services import atualizar_inadimplencia

    cobranca = cobrado.cobranca_franquia
    Cobranca.objects.filter(pk=cobranca.pk).update(
        status=Cobranca.Status.ATRASADO, vencimento=HOJE - timedelta(days=30)
    )
    atualizar_inadimplencia(carla)
    carla.refresh_from_db()
    assert carla.status == Cliente.Status.INADIMPLENTE
    services.cancelar_cobranca_franquia(cobrado)
    carla.refresh_from_db()
    assert carla.status != Cliente.Status.INADIMPLENTE


def test_mesmo_bo_com_acento_e_pontuacao(veiculo):
    _sinistro(veiculo, boletim_ocorrencia="BO 2026-000.123")
    form = SinistroForm(
        _dados(
            veiculo, data=(HOJE - timedelta(days=3)).isoformat(), boletim_ocorrencia="2026000123"
        )
    )
    assert not form.is_valid()
    assert "já foi cadastrado" in str(form.errors)
