"""Qualidade final (multas): cliente recalculado ao corrigir data/carro, ND
congela o responsável, FICI de multas anuladas, atalho "Cobrar" sem cliente,
ações escondidas em multa anulada, NIC de NIC e "empresa absorve"."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.frota.models import Veiculo
from apps.multas import services
from apps.multas.forms import MultaEdicaoForm, MultaForm
from apps.multas.models import Multa
from apps.pessoas.models import Cliente

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
    veiculo = Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol")
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
    return veiculo


def _multa(veiculo, **campos):
    campos.setdefault("data_infracao", HOJE - timedelta(days=30))
    campos.setdefault("valor", Decimal("100.00"))
    return Multa.objects.create(veiculo=veiculo, **campos)


def _dados(multa, **extra):
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


def test_corrigir_data_recalcula_o_cliente(veiculo, ana, bruno):
    multa = _multa(veiculo)
    assert multa.cliente == bruno
    # o form manda o cliente atual (Bruno) de volta: como não mudou, recalcula
    form = MultaEdicaoForm(
        _dados(multa, data_infracao=(HOJE - timedelta(days=150)).isoformat()), instance=multa
    )
    assert form.is_valid(), form.errors
    form.save()
    multa.refresh_from_db()
    assert multa.cliente == ana


def test_corrigir_data_mantem_cliente_escolhido_a_mao(veiculo):
    carla = Cliente.objects.create(nome="Carla", cpf_cnpj="999.888.777-66")
    multa = _multa(veiculo)
    form = MultaEdicaoForm(
        _dados(multa, data_infracao=(HOJE - timedelta(days=150)).isoformat(), cliente=carla.pk),
        instance=multa,
    )
    assert form.is_valid(), form.errors
    form.save()
    multa.refresh_from_db()
    assert multa.cliente == carla


def test_data_antes_da_compra_vira_vendedor_e_volta(veiculo):
    veiculo.data_aquisicao = HOJE - timedelta(days=250)
    veiculo.save(update_fields=["data_aquisicao"])
    multa = _multa(veiculo)
    form = MultaEdicaoForm(
        _dados(multa, data_infracao=(HOJE - timedelta(days=300)).isoformat()), instance=multa
    )
    assert form.is_valid(), form.errors
    form.save()
    multa.refresh_from_db()
    assert multa.responsavel == Multa.Responsavel.VENDEDOR
    form = MultaEdicaoForm(
        _dados(multa, data_infracao=(HOJE - timedelta(days=30)).isoformat()), instance=multa
    )
    assert form.is_valid(), form.errors
    form.save()
    multa.refresh_from_db()
    assert multa.responsavel == Multa.Responsavel.CLIENTE


def test_multa_em_nd_nao_vira_responsabilidade_da_empresa(veiculo, bruno):
    multa = _multa(veiculo)
    services.gerar_nd_de_multas(bruno, [multa], HOJE)
    form = MultaEdicaoForm(_dados(multa, responsavel="empresa"), instance=multa)
    assert not form.is_valid()
    assert "cancele a nota de débito" in str(form.errors["responsavel"])


def test_multa_em_nd_nao_troca_cliente_ao_corrigir_data(veiculo, bruno):
    multa = _multa(veiculo)
    services.gerar_nd_de_multas(bruno, [multa], HOJE)
    form = MultaEdicaoForm(
        _dados(multa, data_infracao=(HOJE - timedelta(days=150)).isoformat()), instance=multa
    )
    assert form.is_valid(), form.errors
    form.save()
    multa.refresh_from_db()
    assert multa.cliente == bruno


def test_alerta_fici_ignora_multas_anuladas(veiculo):
    _multa(veiculo, descricao="valendo", fici_prazo=HOJE)
    _multa(veiculo, descricao="cancelada", fici_prazo=HOJE, resultado=Multa.Resultado.CANCELADA)
    _multa(veiculo, descricao="advertencia", fici_prazo=HOJE, resultado=Multa.Resultado.ADVERTENCIA)
    assert [m.descricao for m in services.alertas_fici(HOJE)] == ["valendo"]
    assert list(Multa.objects.fici_pendente().values_list("descricao", flat=True)) == ["valendo"]


def test_atalho_cobrar_ignora_multa_sem_cliente(db):
    sem_locacao = Veiculo.objects.create(placa="ABC1D23", marca_modelo="Onix")
    sem_cliente = _multa(sem_locacao, fici_status=Multa.Fici.INDICADO)
    assert sem_cliente.cliente is None
    assert sem_cliente.repasse == "Sem cliente: definir responsável"
    condutor = _multa(
        sem_locacao, responsavel=Multa.Responsavel.CONDUTOR, fici_status=Multa.Fici.INDICADO
    )
    assert condutor.repasse == "Sem cliente: definir responsável"
    contas = services.contar_tarefas(Multa.objects.all(), HOJE)
    assert contas["cobrar"] == 0
    assert contas["definir"] == 2
    assert contas["concluidas"] == 0


def test_lista_mostra_atalho_definir_so_quando_ha(usuario_logado, veiculo):
    _multa(veiculo)
    assert "Definir responsável" not in usuario_logado.get("/multas/").content.decode()
    sem_locacao = Veiculo.objects.create(placa="ABC1D23", marca_modelo="Onix")
    _multa(sem_locacao)
    resposta = usuario_logado.get("/multas/?tarefa=definir")
    html = resposta.content.decode()
    assert "Definir responsável" in html
    assert "ABC1D23" in html


def test_multa_anulada_nao_mostra_acoes(usuario_logado, veiculo):
    _multa(
        veiculo,
        fici_prazo=HOJE - timedelta(days=5),
        resultado=Multa.Resultado.CANCELADA,
    )
    html = usuario_logado.get("/multas/").content.decode()
    assert "Nada a pagar" in html
    assert "Marcar paga" not in html
    assert "Já indiquei o condutor" not in html
    assert "Registrar NIC" not in html


def test_nic_recusada_para_multa_anulada_e_para_nic(usuario_logado, veiculo):
    anulada = _multa(
        veiculo, fici_prazo=HOJE - timedelta(days=5), resultado=Multa.Resultado.CANCELADA
    )
    resposta = usuario_logado.post(f"/multas/{anulada.pk}/nic/", follow=True)
    assert "não há NIC a registrar" in resposta.content.decode()
    assert not Multa.objects.filter(multa_origem_nic=anulada).exists()

    original = _multa(veiculo, fici_prazo=HOJE - timedelta(days=5))
    nic = services.registrar_nic(original, valor=Decimal("195.30"))
    resposta = usuario_logado.post(f"/multas/{nic.pk}/nic/", follow=True)
    assert "não há NIC a registrar" in resposta.content.decode()
    assert not Multa.objects.filter(multa_origem_nic=nic).exists()
    with pytest.raises(Exception, match="pendente"):
        services.registrar_nic(nic)


def test_indicar_condutor_recusado_em_multa_anulada(usuario_logado, veiculo):
    anulada = _multa(veiculo, resultado=Multa.Resultado.ADVERTENCIA)
    usuario_logado.post(f"/multas/{anulada.pk}/fici/")
    anulada.refresh_from_db()
    assert anulada.fici_status == Multa.Fici.PENDENTE


def test_empresa_absorve_exige_empresa_em_quem_paga(veiculo):
    dados = {
        "veiculo": veiculo.pk,
        "data_infracao": (HOJE - timedelta(days=3)).isoformat(),
        "valor": "100,00",
        "tipo_condutor": "empresa",
        "responsavel": "cliente",
    }
    form = MultaForm(dados)
    assert not form.is_valid()
    assert "empresa absorve" in str(form.errors["responsavel"])
    form = MultaForm({**dados, "responsavel": "empresa"})
    assert form.is_valid(), form.errors
    # o Alpine da tela troca "Quem vai pagar" sozinho
    assert "mexeuResp" in str(form["tipo_condutor"])


# --- revisão adversarial ---


def test_corrigir_data_da_nic_nao_troca_o_motorista_da_original(veiculo, ana):
    original = _multa(veiculo, data_infracao=HOJE - timedelta(days=150), fici_prazo=HOJE)
    assert original.cliente == ana
    nic = services.registrar_nic(original, valor=Decimal("200.00"))
    assert nic.cliente == ana
    form = MultaEdicaoForm(
        _dados(nic, data_infracao=(HOJE - timedelta(days=10)).isoformat(), valor="200,00"),
        instance=nic,
    )
    assert form.is_valid(), form.errors
    form.save()
    nic.refresh_from_db()
    assert nic.cliente == ana  # continua quem causou a NIC


def test_corrigir_data_recusa_condutor_do_cliente_antigo(veiculo, bruno):
    from apps.pessoas.models import CondutorAutorizado

    irmao = CondutorAutorizado.objects.create(nome="Irmão do Bruno", cliente=bruno)
    multa = _multa(veiculo, tipo_condutor="outro", condutor_autorizado=irmao)
    form = MultaEdicaoForm(
        _dados(
            multa,
            data_infracao=(HOJE - timedelta(days=150)).isoformat(),
            condutor_autorizado=irmao.pk,
        ),
        instance=multa,
    )
    assert not form.is_valid()
    assert "Ana" in str(form.errors["condutor_autorizado"])
