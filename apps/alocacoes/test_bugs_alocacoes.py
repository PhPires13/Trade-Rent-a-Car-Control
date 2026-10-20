"""Bugs da frente alocações: C02, C13, C17, C27, C30, D03, D10, D15, D17, D27, D28."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.contrib.admin.sites import site
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory
from django.urls import reverse

from apps.alocacoes.entrega import situacao_da_devolucao
from apps.alocacoes.models import Alocacao, TrocaTemporaria, Vistoria
from apps.alocacoes.services import linha_do_tempo
from apps.campos import CampoDinheiro
from apps.financeiro import services as financeiro
from apps.financeiro.models import Caucao, Cobranca, MovimentacaoCaucao, MovimentoCredito
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente

HOJE = date.today()


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_atual=50_000)


@pytest.fixture
def substituto(db):
    return Veiculo.objects.create(placa="RUJ3I28", marca_modelo="Onix", km_atual=40_000)


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


@pytest.fixture
def alocacao(veiculo, cliente):
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=14),
        valor_semanal=Decimal("650.00"),
        dia_vencimento=HOJE.weekday(),
        km_entrega=50_000,
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


def _pagar(cobranca, valor, data=HOJE):
    return financeiro.registrar_recebimento(
        cobranca.cliente, data, Decimal(valor), "pix", [(cobranca, Decimal(valor))]
    )


# --- C02: data de devolução no futuro -----------------------------------------


def test_c02_encerrar_recusa_data_futura(alocacao, veiculo):
    with pytest.raises(ValidationError, match="não pode ser no futuro"):
        alocacao.encerrar(HOJE + timedelta(days=30), km_devolucao=51_000)
    alocacao.refresh_from_db()
    veiculo.refresh_from_db()
    assert alocacao.status == Alocacao.Status.ATIVA
    assert veiculo.status == Veiculo.Status.ALOCADO


def test_c02_devolver_substituto_recusa_data_futura(alocacao, substituto):
    troca = TrocaTemporaria.objects.create(
        alocacao=alocacao, veiculo_substituto=substituto, data_retirada=HOJE, km_retirada=40_000
    )
    with pytest.raises(ValidationError, match="não pode ser no futuro"):
        troca.devolver(HOJE + timedelta(days=1), km_devolucao=40_100)


def test_c02_telas_limitam_a_data_a_hoje(usuario_logado, alocacao, substituto):
    hoje = HOJE.isoformat()
    assert f'max="{hoje}"' in usuario_logado.get(f"/alocacoes/{alocacao.pk}/encerrar/").text
    troca = TrocaTemporaria.objects.create(
        alocacao=alocacao, veiculo_substituto=substituto, data_retirada=HOJE, km_retirada=40_000
    )
    assert f'max="{hoje}"' in usuario_logado.get(f"/alocacoes/troca/{troca.pk}/devolver/").text
    resposta = usuario_logado.post(
        f"/alocacoes/troca/{troca.pk}/devolver/",
        {"data_devolucao": (HOJE + timedelta(days=3)).isoformat(), "km_devolucao": "40100"},
    )
    assert "não pode ser no futuro" in resposta.text
    troca.refresh_from_db()
    assert troca.data_devolucao is None


# --- C13: início antes da devolução do motorista anterior ----------------------


def test_c13_nova_alocacao_nao_comeca_antes_do_termino_anterior(alocacao, veiculo, db):
    alocacao.encerrar(HOJE - timedelta(days=2), km_devolucao=51_000)
    outra = Cliente.objects.create(nome="Maria", cpf_cnpj="999.888.777-66")
    nova = Alocacao(
        veiculo=veiculo,
        cliente=outra,
        data_inicio=HOJE - timedelta(days=5),
        valor_semanal=650,
        dia_vencimento=HOJE.weekday(),
        km_entrega=51_000,
    )
    with pytest.raises(ValidationError) as erro:
        nova.full_clean()
    assert "data_inicio" in erro.value.message_dict
    nova.data_inicio = HOJE - timedelta(days=2)  # no mesmo dia da devolução, pode
    nova.full_clean()


def test_c13_troca_nao_comeca_antes_da_locacao(alocacao, substituto):
    troca = TrocaTemporaria(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=alocacao.data_inicio - timedelta(days=1),
        km_retirada=40_000,
    )
    with pytest.raises(ValidationError) as erro:
        troca.full_clean()
    assert "data_retirada" in erro.value.message_dict


def test_c13_substituto_nao_sai_antes_de_voltar_do_motorista_anterior(alocacao, substituto, db):
    outra = Cliente.objects.create(nome="Maria", cpf_cnpj="999.888.777-66")
    anterior = Alocacao.objects.create(
        veiculo=substituto,
        cliente=outra,
        data_inicio=HOJE - timedelta(days=20),
        valor_semanal=650,
        km_entrega=40_000,
    )
    anterior.encerrar(HOJE - timedelta(days=3), km_devolucao=40_500)
    troca = TrocaTemporaria(
        alocacao=alocacao,
        veiculo_substituto=Veiculo.objects.get(pk=substituto.pk),
        data_retirada=HOJE - timedelta(days=6),
        km_retirada=40_500,
    )
    with pytest.raises(ValidationError) as erro:
        troca.full_clean()
    assert "data_retirada" in erro.value.message_dict


# --- C17: arquivos da vistoria e entrega de mídia ------------------------------


def test_c17_vistoria_recusa_html(usuario_logado, alocacao):
    arquivo = SimpleUploadedFile("checklist.html", b"<script>alert(1)</script>", "text/html")
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/vistoria/nova/",
        {"tipo": "entrada", "data": HOJE.isoformat(), "foto": arquivo},
    )
    assert resposta.status_code == 200
    assert not Vistoria.objects.exists()


def test_c17_vistoria_recusa_arquivo_maior_que_10mb(usuario_logado, alocacao):
    arquivo = SimpleUploadedFile(
        "foto.jpg", b"\xff\xd8\xff" + b"0" * (10 * 1024 * 1024), "image/jpeg"
    )
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/vistoria/nova/",
        {"tipo": "entrada", "data": HOJE.isoformat(), "foto": arquivo},
    )
    assert "máximo 10 MB" in resposta.text
    assert not Vistoria.objects.exists()


def test_c17_midia_baixa_html_e_mostra_foto(usuario_logado, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    (tmp_path / "vistorias").mkdir()
    (tmp_path / "vistorias" / "x.html").write_text("<script>alert(1)</script>")
    (tmp_path / "vistorias" / "x.svg").write_text("<svg><script>alert(1)</script></svg>")
    (tmp_path / "vistorias" / "x.jpg").write_bytes(b"\xff\xd8\xff")
    for nome in ("x.html", "x.svg"):
        resposta = usuario_logado.get(f"/midia/vistorias/{nome}")
        assert resposta["Content-Disposition"].startswith("attachment")
        assert resposta["Content-Type"] == "application/octet-stream"
    foto = usuario_logado.get("/midia/vistorias/x.jpg")
    assert foto["Content-Type"] == "image/jpeg"
    assert not foto.get("Content-Disposition", "").startswith("attachment")


def test_c17_campo_de_arquivo_de_outros_formularios_tambem_tem_limite(db):
    from apps.pessoas.views import ClienteForm

    grande = SimpleUploadedFile("cnh.jpg", b"0" * (10 * 1024 * 1024 + 1), "image/jpeg")
    form = ClienteForm(data={}, files={"cnh_frente": grande})
    form.is_valid()
    assert "máximo 10 MB" in str(form.errors.get("cnh_frente"))


# --- C27: vistoria na linha do tempo -------------------------------------------


def test_c27_vistoria_aparece_na_linha_do_tempo(usuario_logado, alocacao, veiculo):
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/vistoria/nova/",
        {"tipo": "saida", "data": HOJE.isoformat(), "km": "50300", "avarias": "Risco na porta"},
        follow=True,
    )
    assert "Risco na porta" in resposta.text
    eventos = linha_do_tempo(veiculo)
    assert any(e["tipo"].startswith("Vistoria") for e in eventos)


# --- C30: telas abertas da ficha voltam para a ficha ----------------------------


def test_c30_editar_e_troca_voltam_para_a_ficha(usuario_logado, alocacao, substituto, cliente):
    ficha = reverse("pessoas:detalhe", args=[cliente.pk])
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/editar/",
        {"valor_semanal": "700,00", "dia_vencimento": HOJE.weekday(), "limite_km": "ilimitado"},
    )
    assert resposta.status_code == 302 and resposta.url == ficha
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/troca/",
        {
            "veiculo_substituto": substituto.pk,
            "data_retirada": HOJE.isoformat(),
            "km_retirada": "40000",
        },
    )
    assert resposta.status_code == 302 and resposta.url == ficha
    troca = TrocaTemporaria.objects.get()
    resposta = usuario_logado.post(
        f"/alocacoes/troca/{troca.pk}/devolver/",
        {"data_devolucao": HOJE.isoformat(), "km_devolucao": "40100"},
    )
    assert resposta.status_code == 302 and resposta.url == ficha


# --- D03: semana não usada já paga vira crédito --------------------------------


def test_d03_semana_paga_no_mes_vira_credito(alocacao):
    semana = _aluguel(alocacao, HOJE)
    _pagar(semana, "650.00")
    alocacao.encerrar(HOJE, km_devolucao=51_000)
    semana.refresh_from_db()
    assert semana.status == Cobranca.Status.CANCELADA
    assert semana.total_quitado == 0
    assert MovimentoCredito.saldo_do_cliente(alocacao.cliente) == Decimal("650.00")
    assert alocacao.acerto_semanas_nao_usadas["credito"] == Decimal("650.00")


def test_d03_semana_parcial_e_cancelada_e_nao_deixa_divida(alocacao, cliente):
    semana = _aluguel(alocacao, HOJE)
    _pagar(semana, "100.00")
    alocacao.encerrar(HOJE, km_devolucao=51_000)
    semana.refresh_from_db()
    assert semana.status == Cobranca.Status.CANCELADA
    assert MovimentoCredito.saldo_do_cliente(cliente) == Decimal("100.00")
    devido = Cobranca.objects.filter(cliente=cliente, status__in=Cobranca.STATUS_DEVIDOS)
    assert not devido.exists()


def test_d03_desconto_de_caucao_volta_para_a_caucao(alocacao):
    caucao = financeiro.abrir_caucao(alocacao, Decimal("1000.00"), HOJE, "pix")
    semana = _aluguel(alocacao, HOJE)
    financeiro.descontar_da_caucao(caucao, semana, Decimal("650.00"), HOJE)
    alocacao.encerrar(HOJE, km_devolucao=51_000)
    caucao = Caucao.com_saldos_anotados(Caucao.objects.filter(pk=caucao.pk)).get()
    assert caucao.saldo == Decimal("1000.00")
    assert not MovimentacaoCaucao.objects.filter(tipo=MovimentacaoCaucao.Tipo.DESCONTO).exists()


def test_d03_pagamento_de_mes_anterior_nao_muda_o_das(alocacao):
    semana = _aluguel(alocacao, HOJE)
    mes_passado = HOJE.replace(day=1) - timedelta(days=1)
    _pagar(semana, "100.00", data=mes_passado)
    antes = financeiro.resumo_fiscal(mes_passado.year, mes_passado.month)["locacao"]
    alocacao.encerrar(HOJE, km_devolucao=51_000)
    semana.refresh_from_db()
    assert semana.valor == Decimal("100.00") and semana.status == Cobranca.Status.PAGO
    assert financeiro.resumo_fiscal(mes_passado.year, mes_passado.month)["locacao"] == antes
    assert alocacao.acerto_semanas_nao_usadas["mes_anterior"] == Decimal("100.00")


def test_d03_tela_de_devolucao_nao_cobra_a_semana_que_sera_cancelada(alocacao):
    _aluguel(alocacao, HOJE - timedelta(days=7))
    _aluguel(alocacao, HOJE)
    situacao = situacao_da_devolucao(alocacao)
    assert situacao["debitos"] == Decimal("650.00")
    assert [c.vencimento for c in situacao["semanas_a_cancelar"]] == [HOJE]


def test_d03_encerrar_pela_tela_avisa_o_credito(usuario_logado, alocacao):
    semana = _aluguel(alocacao, HOJE)
    _pagar(semana, "650.00")
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/encerrar/",
        {"data_termino": HOJE.isoformat(), "km_devolucao": "51000"},
        follow=True,
    )
    assert "viraram crédito" in resposta.text


# --- D10: vistoria durante a troca é do substituto -----------------------------


@pytest.fixture
def troca(alocacao, substituto):
    return TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=HOJE - timedelta(days=3),
        km_retirada=40_000,
    )


def test_d10_vistoria_na_troca_vai_para_o_substituto(usuario_logado, alocacao, troca, veiculo):
    usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/vistoria/nova/",
        {"tipo": "saida", "data": HOJE.isoformat(), "km": "40600"},
    )
    vistoria = Vistoria.objects.get()
    assert vistoria.veiculo_id == troca.veiculo_substituto_id
    veiculo.refresh_from_db()
    troca.veiculo_substituto.refresh_from_db()
    assert veiculo.km_atual == 50_000
    assert troca.veiculo_substituto.km_atual == 40_600
    # não conta como devolução do carro da locação
    assert situacao_da_devolucao(alocacao)["vistoria_devolucao"] is None


def test_d10_checklist_impresso_mostra_o_substituto(usuario_logado, alocacao, troca):
    texto = usuario_logado.get(f"/alocacoes/{alocacao.pk}/vistoria/imprimir/").text
    assert "Onix — RUJ3I28" in texto
    texto = usuario_logado.get(
        f"/alocacoes/{alocacao.pk}/vistoria/imprimir/?veiculo={alocacao.veiculo_id}"
    ).text
    assert "Gol — QXQ6C10" in texto


def test_d10_vistoria_nao_aceita_carro_de_fora_da_locacao(alocacao, db):
    outro = Veiculo.objects.create(placa="ABC1D23", marca_modelo="HB20")
    vistoria = Vistoria(alocacao=alocacao, veiculo=outro, tipo="entrada", data=HOJE)
    with pytest.raises(ValidationError):
        vistoria.full_clean()


# --- D15: Admin não mexe no ciclo de vida --------------------------------------


def test_d15_admin_sem_apagar_criar_nem_mudar_status(alocacao, troca, django_user_model):
    request = RequestFactory().get("/")
    request.user = django_user_model.objects.create_superuser("admin", "a@a.com", "x")
    for modelo, obj, travados in [
        (Alocacao, alocacao, {"status", "data_termino", "km_devolucao", "veiculo"}),
        (TrocaTemporaria, troca, {"data_devolucao", "km_devolucao", "veiculo_substituto"}),
    ]:
        admin = site._registry[modelo]
        assert not admin.has_delete_permission(request, obj)
        assert not admin.has_add_permission(request)
        assert travados <= set(admin.get_readonly_fields(request, obj))
        assert admin.revert_disabled(request, obj)


# --- D17: cliente devendo pega carro só com confirmação ------------------------


def _post_nova(usuario_logado, veiculo, cliente, **extra):
    dados = {
        "veiculo": veiculo.pk,
        "cliente": cliente.pk,
        "data_inicio": HOJE.isoformat(),
        "valor_semanal": "650,00",
        "km_entrega": "50000",
        "limite_km": "ilimitado",
        **extra,
    }
    return usuario_logado.post("/alocacoes/nova/", dados)


def test_d17_inadimplente_precisa_confirmar(usuario_logado, cliente, db):
    carro = Veiculo.objects.create(placa="ABC1D23", marca_modelo="HB20", km_atual=50_000)
    Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.ALUGUEL,
        descricao="Aluguel antigo",
        valor=Decimal("650.00"),
        vencimento=HOJE - timedelta(days=30),
        status=Cobranca.Status.JUDICIAL,
    )
    cliente.status = Cliente.Status.INADIMPLENTE
    cliente.save()
    resposta = _post_nova(usuario_logado, carro, cliente)
    assert resposta.status_code == 200
    assert "R$ 650,00" in resposta.text and "cobrança judicial" in resposta.text
    assert not Alocacao.objects.exists()
    resposta = _post_nova(usuario_logado, carro, cliente, confirmar_divida="on")
    assert resposta.status_code == 302
    assert Alocacao.objects.filter(cliente=cliente).exists()


def test_d17_cliente_em_dia_nao_precisa_confirmar(usuario_logado, cliente, veiculo):
    assert _post_nova(usuario_logado, veiculo, cliente).status_code == 302


# --- D27: KM fora do normal -----------------------------------------------------


def test_d27_vistoria_com_km_absurdo_pede_confirmacao(usuario_logado, alocacao, veiculo):
    url = f"/alocacoes/{alocacao.pk}/vistoria/nova/"
    dados = {"tipo": "saida", "data": HOJE.isoformat(), "km": "500000"}
    resposta = usuario_logado.post(url, dados)
    assert "Confirmo o KM" in resposta.text
    assert not Vistoria.objects.exists()
    resposta = usuario_logado.post(url, {**dados, "confirmar_km": "on"})
    assert resposta.status_code == 302
    veiculo.refresh_from_db()
    assert veiculo.km_atual == 500_000


def test_d27_encerrar_com_km_absurdo_pede_confirmacao(usuario_logado, alocacao):
    url = f"/alocacoes/{alocacao.pk}/encerrar/"
    dados = {"data_termino": HOJE.isoformat(), "km_devolucao": "510000"}
    resposta = usuario_logado.post(url, dados)
    assert "Confirmo o KM" in resposta.text
    alocacao.refresh_from_db()
    assert alocacao.status == Alocacao.Status.ATIVA
    usuario_logado.post(url, {**dados, "confirmar_km": "1"})
    alocacao.refresh_from_db()
    assert alocacao.status == Alocacao.Status.ENCERRADA


def test_d27_km_gigante_vira_mensagem_e_nao_erro_500(usuario_logado, alocacao):
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/encerrar/",
        {"data_termino": HOJE.isoformat(), "km_devolucao": "99999999999", "confirmar_km": "1"},
    )
    assert resposta.status_code == 200
    assert "confira os dígitos" in resposta.text


def test_d27_corrigir_km_da_vistoria_baixa_o_km_do_carro(alocacao, veiculo):
    vistoria = Vistoria.objects.create(alocacao=alocacao, tipo="saida", data=HOJE, km=452_300)
    veiculo.refresh_from_db()
    assert veiculo.km_atual == 452_300
    vistoria.km = 45_230 + 5_000
    vistoria.save()
    veiculo.refresh_from_db()
    assert veiculo.km_atual == 50_230


# --- D28: valores negativos ou zero --------------------------------------------


def test_d28_campo_dinheiro_recusa_negativo():
    with pytest.raises(ValidationError, match="não pode ser negativo"):
        CampoDinheiro().clean("-650,00")
    assert CampoDinheiro(permitir_negativo=True).clean("-1,00") == Decimal("-1.00")


@pytest.mark.parametrize(
    "campo,valor",
    [
        ("valor_semanal", "-650"),
        ("valor_semanal", "0"),
        ("caucao_valor", "-1"),
        ("taxa_km_excedido", "-0,50"),
    ],
)
def test_d28_nova_alocacao_recusa_negativo_e_semanal_zero(
    usuario_logado, veiculo, cliente, campo, valor
):
    resposta = _post_nova(usuario_logado, veiculo, cliente, **{campo: valor})
    assert resposta.status_code == 200
    assert not Alocacao.objects.exists()


def test_d28_editar_recusa_valor_semanal_zero(usuario_logado, alocacao):
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/editar/",
        {"valor_semanal": "0", "dia_vencimento": HOJE.weekday(), "limite_km": "ilimitado"},
    )
    assert resposta.status_code == 200
    alocacao.refresh_from_db()
    assert alocacao.valor_semanal == Decimal("650.00")


# --- revisão adversarial: reforços -----------------------------------------------


def test_d03_encerrar_cancela_encargo_e_tira_a_inadimplencia(alocacao, cliente):
    """Semana atrasada que deixa de ser devida não deixa encargo nem inadimplência."""
    semana = _aluguel(alocacao, HOJE - timedelta(days=7))
    semana.status = Cobranca.Status.ATRASADO
    semana.save(update_fields=["status"])
    encargo = Cobranca.objects.create(
        cliente=cliente,
        alocacao=alocacao,
        origem=Cobranca.Origem.ENCARGO,
        descricao="Encargo",
        valor=Decimal("32.50"),
        vencimento=HOJE - timedelta(days=5),
        status=Cobranca.Status.ATRASADO,
        cobranca_origem=semana,
    )
    Cliente.objects.filter(pk=cliente.pk).update(status=Cliente.Status.INADIMPLENTE)
    alocacao.encerrar(HOJE - timedelta(days=7), km_devolucao=51_000)
    semana.refresh_from_db()
    encargo.refresh_from_db()
    cliente.refresh_from_db()
    assert semana.status == Cobranca.Status.CANCELADA
    assert encargo.status == Cobranca.Status.CANCELADA
    assert cliente.status == Cliente.Status.ATIVO


def test_d03_tela_de_devolucao_nao_cobra_semana_de_transicao(alocacao):
    """Semana que vence antes mas começa no dia da devolução (D04) também será cancelada."""
    semana = _aluguel(alocacao, HOJE - timedelta(days=1))
    semana.periodo_inicio = HOJE
    semana.save(update_fields=["periodo_inicio"])
    situacao = situacao_da_devolucao(alocacao)
    assert situacao["debitos"] == 0
    assert situacao["semanas_a_cancelar"] == [semana]


def test_d10_recalculo_do_km_ignora_vistoria_do_substituto(alocacao, troca, veiculo, substituto):
    """Corrigir a vistoria do carro da locação não puxa o KM do substituto para ele."""
    Vistoria.objects.create(
        alocacao=alocacao, veiculo=substituto, tipo="saida", data=HOJE, km=55_000
    )
    errada = Vistoria.objects.create(alocacao=alocacao, tipo="saida", data=HOJE, km=452_300)
    errada.km = 50_500
    errada.save()
    veiculo.refresh_from_db()
    substituto.refresh_from_db()
    assert veiculo.km_atual == 50_500
    assert substituto.km_atual == 55_000
