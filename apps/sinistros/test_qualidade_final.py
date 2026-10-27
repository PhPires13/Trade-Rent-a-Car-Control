"""Qualidade final (sinistros): franquia já cobrada trava o que a cobrança usou,
franquia exige a data do evento, data do roubo corrigida, lembrete/botão da
franquia na lista, datas do auxílio e da recuperação, ?volta= sem barra,
cadastro sem "Situação" e admin sem N+1."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.financeiro.models import Cobranca
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente
from apps.sinistros import services
from apps.sinistros.models import AuxilioMotorista, Sinistro
from apps.sinistros.views import SinistroForm

HOJE = timezone.localdate()


@pytest.fixture
def ana(db):
    return Cliente.objects.create(nome="Ana", cpf_cnpj="111.222.333-44")


@pytest.fixture
def bruno(db):
    return Cliente.objects.create(nome="Bruno", cpf_cnpj="555.666.777-88")


@pytest.fixture
def veiculo(ana, bruno):
    """Ana ficou com o carro de -200 a -101; Bruno de -100 em diante."""
    veiculo = Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_atual=2000)
    Alocacao.objects.create(
        veiculo=veiculo,
        cliente=ana,
        data_inicio=HOJE - timedelta(days=200),
        data_termino=HOJE - timedelta(days=101),
        status=Alocacao.Status.ENCERRADA,
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
        km_devolucao=1000,
    )
    Alocacao.objects.create(
        veiculo=veiculo,
        cliente=bruno,
        data_inicio=HOJE - timedelta(days=100),
        valor_semanal=Decimal("650.00"),
        km_entrega=1000,
    )
    veiculo.status = Veiculo.Status.ALOCADO
    veiculo.save(update_fields=["status"])
    return veiculo


def _sinistro(veiculo, **campos):
    campos.setdefault("data", HOJE - timedelta(days=10))
    campos.setdefault("envolvido", Sinistro.Envolvido.ASSOCIADO)
    return Sinistro.objects.create(veiculo=veiculo, **campos)


def _dados(sinistro, **extra):
    dados = {
        "veiculo": sinistro.veiculo_id,
        "data": sinistro.data.isoformat(),
        "cliente": sinistro.cliente_id or "",
        "tipo": sinistro.tipo,
        "envolvido": sinistro.envolvido,
        "responsabilidade": sinistro.responsabilidade,
        "descricao": sinistro.descricao,
        "boletim_ocorrencia": "",
        "responsavel_custo": sinistro.responsavel_custo,
        "status": sinistro.status,
        "observacoes": "",
    }
    if sinistro.acionou_protecao:
        dados["acionou_protecao"] = "on"
        dados["data_evento"] = sinistro.data_evento.isoformat() if sinistro.data_evento else ""
        dados["franquia_valor"] = (
            f"{sinistro.franquia_valor:.2f}".replace(".", ",") if sinistro.franquia_valor else ""
        )
    dados.update(extra)
    return dados


@pytest.fixture
def cobrado(veiculo):
    sinistro = _sinistro(
        veiculo,
        acionou_protecao=True,
        data_evento=HOJE - timedelta(days=9),
        franquia_valor=Decimal("1000.00"),
        responsavel_custo="cliente",
    )
    services.cobrar_franquia(sinistro)
    sinistro.refresh_from_db()
    return sinistro


@pytest.mark.parametrize(
    "mudanca",
    [
        {"franquia_valor": "2.500,00"},
        {"responsavel_custo": "empresa"},
        {"acionou_protecao": ""},
    ],
)
def test_franquia_cobrada_trava_valor_quem_paga_e_protecao(cobrado, mudanca):
    dados = _dados(cobrado, **mudanca)
    if mudanca.get("acionou_protecao") == "":
        del dados["acionou_protecao"]
    form = SinistroForm(dados, instance=cobrado)
    assert not form.is_valid()
    assert "A franquia já foi cobrada de Bruno" in str(form.errors)
    assert "Cancele a cobrança em Financeiro" in str(form.errors)


def test_franquia_cobrada_trava_data_que_muda_o_motorista(cobrado):
    form = SinistroForm(
        _dados(cobrado, data=(HOJE - timedelta(days=150)).isoformat()), instance=cobrado
    )
    assert not form.is_valid()
    assert "A franquia já foi cobrada de Bruno" in str(form.errors["data"])


def test_franquia_cobrada_deixa_mudar_data_do_mesmo_motorista(cobrado):
    form = SinistroForm(
        _dados(cobrado, data=(HOJE - timedelta(days=20)).isoformat()), instance=cobrado
    )
    assert form.is_valid(), form.errors


def test_franquia_com_cobranca_cancelada_pode_mudar(cobrado):
    Cobranca.objects.filter(pk=cobrado.cobranca_franquia_id).update(status="cancelada")
    cobrado.refresh_from_db()
    form = SinistroForm(_dados(cobrado, franquia_valor="2.500,00"), instance=cobrado)
    assert form.is_valid(), form.errors


def test_franquia_exige_data_do_evento(veiculo):
    sinistro = _sinistro(veiculo)
    form = SinistroForm(
        _dados(sinistro, acionou_protecao="on", franquia_valor="1.500,00", data_evento=""),
        instance=sinistro,
    )
    assert not form.is_valid()
    assert "franquia entra nas despesas desse mês" in str(form.errors["data_evento"])
    # franquia zero (franquia grátis do plano) não exige
    form = SinistroForm(
        _dados(sinistro, acionou_protecao="on", franquia_valor="0", data_evento=""),
        instance=sinistro,
    )
    assert form.is_valid(), form.errors


def test_corrigir_data_do_roubo_avisa_a_locacao(usuario_logado, veiculo):
    data_roubo = HOJE - timedelta(days=10)
    resposta = usuario_logado.post(
        reverse("sinistros:novo"),
        {
            "veiculo": veiculo.pk,
            "data": data_roubo.isoformat(),
            "tipo": Sinistro.Tipo.ROUBO,
            "envolvido": Sinistro.Envolvido.ASSOCIADO,
            "responsabilidade": Sinistro.Responsabilidade.INDEFINIDA,
            "responsavel_custo": "empresa",
            "token": "roubo-1",
        },
    )
    assert resposta.status_code == 302
    sinistro = Sinistro.objects.get()
    assert Alocacao.objects.get(cliente__nome="Bruno").data_termino == data_roubo
    resposta = usuario_logado.post(
        reverse("sinistros:editar", args=[sinistro.pk]),
        _dados(sinistro, data=(HOJE - timedelta(days=8)).isoformat()),
        follow=True,
    )
    html = resposta.content.decode()
    assert f"encerrada em {data_roubo:%d/%m/%Y}" in html
    assert "ajuste o término em Alocações" in html


def test_lista_mostra_franquia_a_cobrar_e_botao(usuario_logado, veiculo):
    _sinistro(
        veiculo,
        acionou_protecao=True,
        data_evento=HOJE - timedelta(days=9),
        franquia_valor=Decimal("1000.00"),
        responsavel_custo="cliente",
    )
    html = usuario_logado.get(reverse("sinistros:lista")).content.decode()
    assert "Franquia de R$" in html
    assert "a cobrar de Bruno" in html
    assert reverse("sinistros:cobrar_franquia", args=[Sinistro.objects.get().pk]) in html


def test_salvar_lembra_da_franquia_a_cobrar(usuario_logado, veiculo):
    sinistro = _sinistro(veiculo)
    resposta = usuario_logado.post(
        reverse("sinistros:editar", args=[sinistro.pk]),
        _dados(
            sinistro,
            acionou_protecao="on",
            data_evento=(HOJE - timedelta(days=9)).isoformat(),
            franquia_valor="1.000,00",
            responsavel_custo="cliente",
        ),
        follow=True,
    )
    assert "a cobrar de Bruno" in resposta.content.decode()


def test_auxilio_recebido_na_data_digitada(usuario_logado, veiculo):
    sinistro = _sinistro(veiculo)
    auxilio = AuxilioMotorista.objects.create(
        sinistro=sinistro, valor=Decimal("1518.00"), status=AuxilioMotorista.Status.SOLICITADO
    )
    url = reverse("sinistros:receber_auxilio", args=[auxilio.pk])
    usuario_logado.post(url, {"data": (HOJE + timedelta(days=2)).isoformat()})
    auxilio.refresh_from_db()
    assert auxilio.status == AuxilioMotorista.Status.SOLICITADO  # futuro recusado
    dia = HOJE - timedelta(days=3)
    usuario_logado.post(url, {"data": dia.isoformat()})
    auxilio.refresh_from_db()
    assert auxilio.status == AuxilioMotorista.Status.RECEBIDO
    assert auxilio.data_recebimento == dia


def test_carro_recuperado_na_data_digitada(usuario_logado, veiculo):
    sinistro = _sinistro(veiculo, tipo=Sinistro.Tipo.ROUBO)
    services.registrar_roubo(sinistro)
    url = reverse("sinistros:carro_recuperado", args=[sinistro.pk])
    usuario_logado.post(url, {"data": (sinistro.data - timedelta(days=1)).isoformat()})
    sinistro.refresh_from_db()
    assert sinistro.data_recuperacao is None  # antes do roubo: recusado
    usuario_logado.post(url, {"data": (HOJE + timedelta(days=1)).isoformat()})
    sinistro.refresh_from_db()
    assert sinistro.data_recuperacao is None  # futuro: recusado
    dia = HOJE - timedelta(days=2)
    usuario_logado.post(url, {"data": dia.isoformat()})
    sinistro.refresh_from_db()
    assert sinistro.data_recuperacao == dia


def test_volta_sem_barra_nao_da_erro(usuario_logado, veiculo):
    sinistro = _sinistro(veiculo)
    resposta = usuario_logado.get(reverse("sinistros:editar", args=[sinistro.pk]) + "?volta=lista")
    assert resposta.status_code == 200
    resposta = usuario_logado.post(
        reverse("sinistros:solicitar_auxilio", args=[sinistro.pk]), {"volta": "lista"}
    )
    assert resposta.status_code == 302
    assert resposta["Location"] == reverse("sinistros:lista")
    resposta = usuario_logado.post(
        reverse("sinistros:solicitar_auxilio", args=[sinistro.pk]), {"volta": "//mal.com/x"}
    )
    assert resposta["Location"] == reverse("sinistros:lista")


def test_cadastro_sem_situacao(usuario_logado, veiculo):
    form = SinistroForm()
    assert "status" not in form.fields
    assert all(bloco["chave"] != "situacao" for bloco in form.blocos())
    html = usuario_logado.get(reverse("sinistros:novo")).content.decode()
    assert "Situação do sinistro" not in html
    editar = SinistroForm(instance=_sinistro(veiculo))
    assert "status" in editar.fields


def test_admin_lista_sem_n_mais_1(admin_client, veiculo, django_assert_max_num_queries):
    for dias in range(5):
        _sinistro(veiculo, data=HOJE - timedelta(days=dias + 1))
    admin_client.get("/admin/sinistros/sinistro/")
    with django_assert_max_num_queries(12):
        assert admin_client.get("/admin/sinistros/sinistro/").status_code == 200
