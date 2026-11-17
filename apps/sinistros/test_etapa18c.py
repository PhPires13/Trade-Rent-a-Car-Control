"""Etapa 18c (sinistros): sobras da etapa 18.

__str__ que não quebra com o carro apagado e R$ formatado no /admin.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.frota.models import Veiculo
from apps.sinistros.models import AuxilioMotorista, Sinistro

HOJE = timezone.localdate()


@pytest.fixture
def carro(db):
    return Veiculo.objects.create(placa="SHN2E34", marca_modelo="Argo")


def _sinistro(veiculo, **campos):
    campos.setdefault("data", HOJE - timedelta(days=10))
    campos.setdefault("envolvido", "associado")
    return Sinistro.objects.create(veiculo=veiculo, **campos)


# --- (3) __str__ com o carro apagado ---


def test_str_do_sinistro_sem_carro_nao_quebra(db):
    sinistro = Sinistro(veiculo_id=999_999, data=HOJE, tipo=Sinistro.Tipo.COLISAO)
    assert str(sinistro) == f"carro apagado — Colisão/avaria em {HOJE:%d/%m/%Y}"


def test_str_do_sinistro_normal_continua_igual(carro):
    sinistro = _sinistro(carro, data=HOJE)
    assert str(sinistro) == f"SHN2E34 — Colisão/avaria em {HOJE:%d/%m/%Y}"


def test_registro_do_historico_do_sinistro_com_carro_apagado_nao_quebra(carro):
    registro = _sinistro(carro).history.first()
    registro.veiculo_id = 999_999
    assert "carro apagado" in str(registro.instance)


def test_str_do_auxilio_sem_sinistro_nao_quebra(db):
    auxilio = AuxilioMotorista(sinistro_id=999_999)
    assert str(auxilio) == "Auxílio — sinistro apagado (A solicitar)"


# --- (4) R$ no /admin ---


def test_admin_de_sinistros_mostra_franquia_em_reais(admin_client, carro):
    _sinistro(carro, franquia_valor=Decimal("1500.00"))
    resposta = admin_client.get(reverse("admin:sinistros_sinistro_changelist"))
    assert resposta.status_code == 200
    assert "R$ 1.500,00" in resposta.content.decode()


def test_admin_de_auxilios_mostra_valor_em_reais(admin_client, carro):
    AuxilioMotorista.objects.create(sinistro=_sinistro(carro), valor=Decimal("1518.00"))
    resposta = admin_client.get(reverse("admin:sinistros_auxiliomotorista_changelist"))
    assert resposta.status_code == 200
    assert "R$ 1.518,00" in resposta.content.decode()


# --- refatoração: salvar só um campo não procura o motorista ---


@pytest.fixture
def carro_com_bia_hoje(db):
    from apps.alocacoes.models import Alocacao
    from apps.pessoas.models import Cliente

    bia = Cliente.objects.create(nome="Bia", cpf_cnpj="222.333.444-55")
    veiculo = Veiculo.objects.create(placa="ROU1B23", marca_modelo="Onix")
    Alocacao.objects.create(
        veiculo=veiculo,
        cliente=bia,
        data_inicio=HOJE - timedelta(days=10),
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
    )
    return veiculo, bia


def test_sinistro_salvo_so_num_campo_nao_procura_o_cliente(
    carro_com_bia_hoje, django_assert_num_queries
):
    veiculo, _ = carro_com_bia_hoje
    sinistro = _sinistro(veiculo, data=HOJE - timedelta(days=60), tipo=Sinistro.Tipo.ROUBO)
    assert sinistro.cliente_id is None
    sinistro.data_recuperacao = HOJE
    with django_assert_num_queries(2):  # UPDATE + registro do histórico
        sinistro.save(update_fields=["data_recuperacao"])
    sinistro.refresh_from_db()
    assert sinistro.cliente_id is None
    assert sinistro.data_recuperacao == HOJE


def test_sinistro_novo_sem_cliente_continua_ganhando_quem_estava_com_o_carro(
    carro_com_bia_hoje,
):
    veiculo, bia = carro_com_bia_hoje
    assert _sinistro(veiculo, data=HOJE - timedelta(days=2)).cliente == bia


# --- valor negativo barrado também pelo /admin e no banco (Etapa 15: sem negativos) ---


def test_admin_recusa_franquia_negativa(carro, admin_user, rf):
    from django.contrib import admin

    pedido = rf.get("/")
    pedido.user = admin_user
    form_admin = admin.site._registry[Sinistro].get_form(pedido)
    dados = {
        "veiculo": carro.pk,
        "data": HOJE.isoformat(),
        "tipo": Sinistro.Tipo.COLISAO,
        "envolvido": "associado",
        "responsabilidade": "indefinida",
        "franquia_valor": "-900",
        "responsavel_custo": "empresa",
        "status": Sinistro.Status.ABERTO,
    }
    form = form_admin(dados)
    assert not form.is_valid()
    assert form.errors["franquia_valor"] == ["O valor não pode ser negativo."]
    dados["franquia_valor"] = "900"
    assert form_admin(dados).is_valid()


def test_auxilio_negativo_e_recusado(carro):
    from django.core.exceptions import ValidationError

    auxilio = AuxilioMotorista(sinistro=_sinistro(carro), valor=Decimal("-1518.00"))
    with pytest.raises(ValidationError) as erro:
        auxilio.full_clean()
    assert erro.value.message_dict["valor"] == ["O valor não pode ser negativo."]


def test_banco_recusa_franquia_e_auxilio_negativos(carro):
    from django.db import IntegrityError, transaction

    sinistro = _sinistro(carro, franquia_valor=Decimal("0"))
    auxilio = AuxilioMotorista.objects.create(sinistro=sinistro, valor=None)
    with pytest.raises(IntegrityError), transaction.atomic():
        Sinistro.objects.filter(pk=sinistro.pk).update(franquia_valor=Decimal("-900"))
    with pytest.raises(IntegrityError), transaction.atomic():
        AuxilioMotorista.objects.filter(pk=auxilio.pk).update(valor=Decimal("-1"))
    Sinistro.objects.filter(pk=sinistro.pk).update(franquia_valor=None)
    AuxilioMotorista.objects.filter(pk=auxilio.pk).update(valor=Decimal("0"))


# --- um auxílio por sinistro também pelo /admin e no banco ---


def test_banco_recusa_segundo_auxilio_no_mesmo_sinistro(carro):
    from django.db import IntegrityError, transaction

    sinistro = _sinistro(carro)
    AuxilioMotorista.objects.create(sinistro=sinistro, valor=Decimal("1518.00"))
    with pytest.raises(IntegrityError), transaction.atomic():
        AuxilioMotorista.objects.create(sinistro=sinistro, valor=Decimal("300.00"))
    AuxilioMotorista.objects.create(sinistro=_sinistro(carro), valor=Decimal("300.00"))
    assert AuxilioMotorista.objects.count() == 2


def test_admin_de_auxilio_recusa_segundo_no_mesmo_sinistro(carro, admin_user, rf):
    from django.contrib import admin

    sinistro = _sinistro(carro)
    AuxilioMotorista.objects.create(sinistro=sinistro, valor=Decimal("1518.00"))
    pedido = rf.get("/")
    pedido.user = admin_user
    form_admin = admin.site._registry[AuxilioMotorista].get_form(pedido)
    form = form_admin({"sinistro": sinistro.pk, "valor": "300", "status": "solicitado"})
    assert not form.is_valid()
    # aparece no topo do formulário do /admin
    assert form.non_field_errors() == ["Este sinistro já tem auxílio registrado."]


def test_admin_do_sinistro_mostra_no_maximo_um_auxilio(carro, admin_user, rf):
    from django.contrib import admin

    pedido = rf.get("/")
    pedido.user = admin_user
    (inline,) = admin.site._registry[Sinistro].get_inline_instances(pedido)
    assert inline.get_formset(pedido, _sinistro(carro)).max_num == 1


def test_pedir_auxilio_duas_vezes_continua_avisando(usuario_logado, carro):
    from django.contrib.messages import get_messages

    from apps.manutencao.models import Manutencao

    sinistro = _sinistro(carro, acionou_protecao=True, data=HOJE - timedelta(days=30))
    Manutencao.objects.create(
        veiculo=carro,
        tipo="corretiva",
        data=HOJE,
        descricao="Funilaria",
        data_entrada=HOJE - timedelta(days=20),
        sinistro=sinistro,
    )
    url = reverse("sinistros:solicitar_auxilio", args=[sinistro.pk])
    usuario_logado.post(url)
    resposta = usuario_logado.post(url)
    assert "O auxílio deste sinistro já estava registrado." in [
        str(m) for m in get_messages(resposta.wsgi_request)
    ]
    assert sinistro.auxilios.count() == 1


# --- revisão: a trava nova do modelo não repete o aviso nas telas ---


def test_tela_do_auxilio_avisa_valor_negativo_uma_vez_so(usuario_logado, carro):
    from django.contrib.messages import get_messages

    auxilio = AuxilioMotorista.objects.create(
        sinistro=_sinistro(carro), status="solicitado", data_solicitacao=HOJE
    )
    resposta = usuario_logado.post(
        reverse("sinistros:receber_auxilio", args=[auxilio.pk]), {"valor": "-1.518,00"}
    )
    assert [str(m) for m in get_messages(resposta.wsgi_request)] == [
        "O valor não pode ser negativo."
    ]
    auxilio.refresh_from_db()
    assert auxilio.status == "solicitado"


def test_tela_do_sinistro_avisa_franquia_negativa_uma_vez_so(carro):
    from apps.sinistros.views import SinistroForm

    form = SinistroForm(
        {
            "veiculo": carro.pk,
            "data": HOJE.isoformat(),
            "tipo": Sinistro.Tipo.COLISAO,
            "envolvido": "associado",
            "responsabilidade": "indefinida",
            "acionou_protecao": "on",
            "franquia_valor": "-900,00",
            "responsavel_custo": "empresa",
        }
    )
    assert not form.is_valid()
    assert form.errors["franquia_valor"] == ["O valor não pode ser negativo."]
    assert not form.non_field_errors()


def test_carro_recuperado_nao_poe_cliente_no_roubo_sem_motorista(carro_com_bia_hoje):
    from apps.sinistros import services

    veiculo, _ = carro_com_bia_hoje
    roubo = _sinistro(veiculo, data=HOJE - timedelta(days=60), tipo=Sinistro.Tipo.ROUBO)
    services.carro_recuperado(roubo, HOJE)
    roubo.refresh_from_db()
    assert roubo.cliente_id is None
    assert roubo.data_recuperacao == HOJE
    assert all(registro.cliente_id is None for registro in roubo.history.all())


# --- franquia com pagamento: o cartão diz o caminho em vez de ficar mudo ---


@pytest.fixture
def franquia_cobrada(db):
    from apps.alocacoes.models import Alocacao
    from apps.pessoas.models import Cliente
    from apps.sinistros import services

    lucas = Cliente.objects.create(nome="Lucas", cpf_cnpj="333.444.555-66")
    veiculo = Veiculo.objects.create(placa="FRQ1A23", marca_modelo="Onix")
    Alocacao.objects.create(
        veiculo=veiculo,
        cliente=lucas,
        data_inicio=HOJE - timedelta(days=60),
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
    )
    sinistro = _sinistro(
        veiculo,
        data=HOJE - timedelta(days=16),
        acionou_protecao=True,
        data_evento=HOJE - timedelta(days=15),
        franquia_valor=Decimal("2500.00"),
        responsavel_custo="cliente",
        responsabilidade=Sinistro.Responsabilidade.CLIENTE,
    )
    services.cobrar_franquia(sinistro)
    sinistro.refresh_from_db()
    return sinistro


def _pagar(cobranca, valor):
    from apps.financeiro.models import AplicacaoRecebimento, Recebimento

    recebimento = Recebimento.objects.create(
        cliente=cobranca.cliente, data=HOJE, valor=valor, forma=Recebimento.Forma.PIX
    )
    AplicacaoRecebimento.objects.create(recebimento=recebimento, cobranca=cobranca, valor=valor)


def test_franquia_com_pagamento_parcial_mostra_o_caminho(usuario_logado, franquia_cobrada):
    cobranca = franquia_cobrada.cobranca_franquia
    _pagar(cobranca, Decimal("100.00"))
    html = usuario_logado.get(
        reverse("sinistros:editar", args=[franquia_cobrada.pk])
    ).content.decode()
    assert "Cancelar esta cobrança</button>" not in html
    assert "Já tem pagamento: para corrigir, desfaça o pagamento" in html
    assert reverse("financeiro:extrato_cliente", args=[cobranca.cliente_id]) in html


def test_franquia_atrasada_com_pagamento_nao_oferece_o_cancelar(usuario_logado, franquia_cobrada):
    """O serviço recusa cancelar com pagamento: o botão não pode aparecer à toa."""
    from apps.financeiro.models import Cobranca

    cobranca = franquia_cobrada.cobranca_franquia
    _pagar(cobranca, Decimal("100.00"))
    Cobranca.objects.filter(pk=cobranca.pk).update(status=Cobranca.Status.ATRASADO)
    html = usuario_logado.get(
        reverse("sinistros:editar", args=[franquia_cobrada.pk])
    ).content.decode()
    assert "Cancelar esta cobrança</button>" not in html
    assert "Já tem pagamento" in html


def test_franquia_judicial_explica_e_aponta_o_extrato(usuario_logado, franquia_cobrada):
    from apps.financeiro.models import Cobranca

    cobranca = franquia_cobrada.cobranca_franquia
    Cobranca.objects.filter(pk=cobranca.pk).update(status=Cobranca.Status.JUDICIAL)
    html = usuario_logado.get(
        reverse("sinistros:editar", args=[franquia_cobrada.pk])
    ).content.decode()
    assert "Cancelar esta cobrança</button>" not in html
    assert "cobrança judicial" in html
    assert reverse("financeiro:extrato_cliente", args=[cobranca.cliente_id]) in html


def test_franquia_sem_pagamento_continua_com_o_cancelar(usuario_logado, franquia_cobrada):
    html = usuario_logado.get(
        reverse("sinistros:editar", args=[franquia_cobrada.pk])
    ).content.decode()
    assert "Cancelar esta cobrança</button>" in html
    assert "data-franquia-travada" not in html
