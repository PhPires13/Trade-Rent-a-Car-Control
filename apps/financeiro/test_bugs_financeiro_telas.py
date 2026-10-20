"""Bugs da frente financeiro-telas: C01, C06, C07, C11, C20, C29, D05, D11."""

import csv
import io
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.contrib import admin

from apps.alocacoes.models import Alocacao
from apps.financeiro import services
from apps.financeiro.models import (
    Caucao,
    Cobranca,
    ItemNotaDebito,
    MovimentacaoCaucao,
    NotaDebito,
    Recebimento,
)
from apps.frota.models import Veiculo
from apps.multas.models import Multa
from apps.pessoas.models import Cliente

HOJE = date.today()


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_atual=50_000)


@pytest.fixture
def alocacao(veiculo, cliente):
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=60),
        valor_semanal=Decimal("650.00"),
        dia_vencimento=2,
        km_entrega=50_000,
    )


def _edicao(**extras):
    dados = {
        "valor_semanal": "650,00",
        "dia_vencimento": "2",
        "caucao_valor": "",
        "limite_km": "ilimitado",
        "franquia_km_mensal": "",
        "taxa_km_excedido": "",
        "observacoes": "",
    }
    dados.update(extras)
    return dados


def _cobranca(cliente, origem=Cobranca.Origem.ALUGUEL, valor="650.00", **kw):
    kw.setdefault("vencimento", HOJE - timedelta(days=10))
    kw.setdefault("status", Cobranca.Status.ATRASADO)
    return Cobranca.objects.create(
        cliente=cliente, origem=origem, descricao="Aluguel semanal", valor=Decimal(valor), **kw
    )


# ---------- C01: caução combinada depois, em "Editar valores" ----------


def test_c01_caucao_posta_na_edicao_abre_o_registro(usuario_logado, alocacao):
    assert not Caucao.objects.filter(alocacao=alocacao).exists()
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/editar/", _edicao(caucao_valor="1.500,00")
    )
    assert resposta.status_code == 302
    assert Caucao.objects.filter(alocacao=alocacao).exists()


def test_c01_nao_tira_a_caucao_com_dinheiro_retido(usuario_logado, alocacao):
    alocacao.caucao_valor = Decimal("1500.00")
    alocacao.save()
    caucao = Caucao.objects.get(alocacao=alocacao)
    MovimentacaoCaucao.objects.create(
        caucao=caucao, tipo="recebimento", valor=Decimal("1500.00"), data=HOJE
    )
    resposta = usuario_logado.post(f"/alocacoes/{alocacao.pk}/editar/", _edicao())
    assert resposta.status_code == 200  # volta a tela com o erro
    assert "retidos" in resposta.content.decode()
    alocacao.refresh_from_db()
    assert alocacao.caucao_valor == Decimal("1500.00")


def test_c01_tirar_caucao_sem_deposito_apaga_o_registro_vazio(usuario_logado, alocacao):
    alocacao.caucao_valor = Decimal("1500.00")
    alocacao.save()
    assert Caucao.objects.filter(alocacao=alocacao).exists()
    usuario_logado.post(f"/alocacoes/{alocacao.pk}/editar/", _edicao())
    assert not Caucao.objects.filter(alocacao=alocacao).exists()


# ---------- C06: clique duplo na tela da caução ----------


def test_c06_clique_duplo_na_caucao_grava_uma_vez(usuario_logado, alocacao):
    caucao = services.abrir_caucao(alocacao)
    html = usuario_logado.get(f"/financeiro/caucoes/{caucao.pk}/").content.decode()
    assert 'name="token"' in html
    dados = {
        "tipo": "recebimento",
        "valor": "1.500,00",
        "data": HOJE.isoformat(),
        "forma": "pix",
        "token": "abc123",
    }
    usuario_logado.post(f"/financeiro/caucoes/{caucao.pk}/", dados)
    resposta = usuario_logado.post(f"/financeiro/caucoes/{caucao.pk}/", dados, follow=True)
    assert caucao.movimentacoes.count() == 1
    assert caucao.saldo == Decimal("1500.00")
    assert "clique duplo" in resposta.content.decode()


# ---------- C07: valores absurdos e descrições longas ----------


@pytest.mark.parametrize("digitado", ["1e10", "999999999", "100000000,00"])
def test_c07_receber_recusa_valor_fora_do_campo(usuario_logado, cliente, digitado):
    _cobranca(cliente)
    resposta = usuario_logado.post(
        "/financeiro/receber/",
        {
            "cliente": cliente.pk,
            "valor": digitado,
            "data": HOJE.isoformat(),
            "forma": "pix",
            "sobra_destino": "credito",
        },
    )
    assert resposta.status_code == 200
    assert not Recebimento.objects.exists()
    assert "99.999.999,99" in resposta.content.decode()


def test_c07_caucao_recusa_valor_fora_do_campo(usuario_logado, alocacao):
    caucao = services.abrir_caucao(alocacao)
    resposta = usuario_logado.post(
        f"/financeiro/caucoes/{caucao.pk}/",
        {"tipo": "recebimento", "valor": "1e10", "data": HOJE.isoformat(), "forma": "pix"},
    )
    assert resposta.status_code == 200
    assert not caucao.movimentacoes.exists()
    assert "99.999.999,99" in resposta.content.decode()


def test_c07_encargo_recusa_valor_fora_do_campo(usuario_logado, cliente):
    atrasada = _cobranca(cliente)
    resposta = usuario_logado.post(
        f"/financeiro/cobranca/{atrasada.pk}/encargo/", {"valor": "1e10"}, follow=True
    )
    assert not Cobranca.objects.filter(origem=Cobranca.Origem.ENCARGO).exists()
    assert "99.999.999,99" in resposta.content.decode()


def test_c07_nd_recusa_valor_gigante_e_descricao_longa(usuario_logado, cliente):
    base = {"cliente": cliente.pk, "data_emissao": HOJE.isoformat(), "token": ""}
    resposta = usuario_logado.post(
        "/financeiro/nd/nova/", {**base, "item_descricao": "Avaria", "item_valor": "1e10"}
    )
    assert resposta.status_code == 200
    assert "99.999.999,99" in resposta.content.decode()
    longa = "Multa " + "x" * 250
    resposta = usuario_logado.post(
        "/financeiro/nd/nova/", {**base, "item_descricao": longa, "item_valor": "150,00"}
    )
    assert resposta.status_code == 200
    html = resposta.content.decode()
    assert "200 letras" in html
    assert longa in html  # o que foi digitado volta para encurtar
    assert 'maxlength="200"' in html
    assert not NotaDebito.objects.exists()


# ---------- C11: planilha do DAS que abre no Excel ----------


def test_c11_das_csv_abre_no_excel_brasileiro(usuario_logado, db):
    resposta = usuario_logado.get(f"/financeiro/das/?mes={HOJE:%Y-%m}&exportar=csv")
    conteudo = resposta.content
    assert conteudo.startswith(b"\xef\xbb\xbf")  # BOM: acentos certos no Excel
    linhas = list(csv.reader(io.StringIO(conteudo.decode("utf-8-sig")), delimiter=";"))
    assert linhas[0] == ["Grupo", "Valor (R$)", "Entra na base do DAS?"]
    assert linhas[1][1] == "0,00"  # vírgula no decimal


def test_c11_das_exporta_xlsx(usuario_logado, db):
    from openpyxl import load_workbook

    resposta = usuario_logado.get(f"/financeiro/das/?mes={HOJE:%Y-%m}&exportar=xlsx")
    assert resposta.status_code == 200
    assert "spreadsheetml" in resposta["Content-Type"]
    assert f"das-{HOJE:%Y-%m}.xlsx" in resposta["Content-Disposition"]
    planilha = load_workbook(io.BytesIO(resposta.content)).active
    assert planilha["A1"].value == "Grupo"
    assert isinstance(planilha["B2"].value, (int, float))


# ---------- C20: encargo e judicial lado a lado ----------


def test_c20_atrasada_sem_encargo_mostra_encargo_e_judicial(usuario_logado, cliente):
    _cobranca(cliente, vencimento=HOJE - timedelta(days=10))
    html = usuario_logado.get("/financeiro/").content.decode()
    assert "Aplicar" in html
    assert "mandar para judicial" in html


# ---------- C29: ações voltam para os filtros e a página ----------


@pytest.mark.parametrize(
    "acao,origem",
    [
        ("cancelar", Cobranca.Origem.EXCEDENTE_KM),
        ("judicial", Cobranca.Origem.ALUGUEL),
        ("encargo", Cobranca.Origem.ALUGUEL),
    ],
)
def test_c29_acao_volta_para_o_filtro(usuario_logado, cliente, acao, origem):
    cobranca = _cobranca(cliente, origem=origem)
    voltar = "status=atrasado&origem=excedente_km&pagina=2"
    resposta = usuario_logado.post(
        f"/financeiro/cobranca/{cobranca.pk}/{acao}/", {"voltar": voltar, "valor": "10,00"}
    )
    assert resposta.status_code == 302
    assert resposta["Location"] == f"/financeiro/?{voltar}"


def test_c29_voltar_nao_leva_para_fora(usuario_logado, cliente):
    cobranca = _cobranca(cliente, origem=Cobranca.Origem.EXCEDENTE_KM)
    resposta = usuario_logado.post(
        f"/financeiro/cobranca/{cobranca.pk}/cancelar/",
        {"voltar": "https://golpe.example.com/?status=x&next=//evil"},
    )
    assert resposta["Location"].startswith("/financeiro/")


def test_c29_formularios_levam_os_filtros(usuario_logado, cliente):
    _cobranca(cliente, origem=Cobranca.Origem.EXCEDENTE_KM)
    html = usuario_logado.get("/financeiro/?status=atrasado&origem=excedente_km").content.decode()
    assert 'name="voltar" value="status=atrasado&amp;origem=excedente_km&amp;pagina=1"' in html


# ---------- D05: cancelar a ND inteira quando a multa é anulada ----------


@pytest.fixture
def nd_de_multa(cliente, veiculo, alocacao):
    multa = Multa.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_infracao=HOJE - timedelta(days=30),
        valor=Decimal("293.47"),
    )
    from apps.multas.services import gerar_nd_de_multas

    nd = gerar_nd_de_multas(cliente, [multa], HOJE, vencimento=HOJE + timedelta(days=7))
    return nd, multa


def test_d05_cancela_nd_sem_pagamento_e_libera_a_multa(usuario_logado, nd_de_multa):
    nd, multa = nd_de_multa
    html = usuario_logado.get("/financeiro/nd/").content.decode()
    assert "cancelar esta ND" in html
    resposta = usuario_logado.post(
        f"/financeiro/cobranca/{nd.cobranca.pk}/cancelar/", {"voltar": "nds"}
    )
    assert resposta["Location"] == "/financeiro/nd/"
    nd.cobranca.refresh_from_db()
    assert nd.cobranca.status == Cobranca.Status.CANCELADA
    multa.refresh_from_db()
    assert multa.repasse == "A cobrar"  # livre para entrar numa ND nova
    # o resultado "cancelada" agora pode ser registrado
    multa.resultado = Multa.Resultado.CANCELADA
    multa.full_clean()
    # a ND continua na lista (número usado), mas como Cancelada
    assert ItemNotaDebito.objects.filter(nota_debito=nd).exists()
    assert "Cancelada" in usuario_logado.get("/financeiro/nd/").content.decode()


def test_d05_nao_cancela_nd_com_pagamento(usuario_logado, nd_de_multa, cliente):
    nd, multa = nd_de_multa
    services.registrar_recebimento(
        cliente=cliente,
        data=HOJE,
        valor=Decimal("100.00"),
        forma=Recebimento.Forma.PIX,
        aplicacoes=[(nd.cobranca, Decimal("100.00"))],
    )
    usuario_logado.post(f"/financeiro/cobranca/{nd.cobranca.pk}/cancelar/")
    nd.cobranca.refresh_from_db()
    assert nd.cobranca.status != Cobranca.Status.CANCELADA
    assert ItemNotaDebito.objects.filter(multa=multa).exists()


def test_d05_nao_cancela_nd_judicial(usuario_logado, nd_de_multa):
    nd, multa = nd_de_multa
    Cobranca.objects.filter(pk=nd.cobranca.pk).update(status=Cobranca.Status.JUDICIAL)
    usuario_logado.post(f"/financeiro/cobranca/{nd.cobranca.pk}/cancelar/")
    nd.cobranca.refresh_from_db()
    assert nd.cobranca.status == Cobranca.Status.JUDICIAL
    assert ItemNotaDebito.objects.filter(multa=multa).exists()


def test_d05_mensagem_da_multa_explica_como_cancelar(nd_de_multa):
    from django.core.exceptions import ValidationError

    _nd, multa = nd_de_multa
    multa.resultado = Multa.Resultado.CANCELADA
    with pytest.raises(ValidationError) as erro:
        multa.full_clean()
    assert "Notas de débito" in str(erro.value)


# ---------- D11: valores financeiros só leitura no Admin ----------


@pytest.mark.parametrize(
    "modelo,campos",
    [(Cobranca, {"valor", "status"}), (Recebimento, {"valor"})],
)
def test_d11_admin_valores_so_leitura(rf, admin_user, modelo, campos, cliente):
    modelo_admin = admin.site._registry[modelo]
    if modelo is Cobranca:
        obj = _cobranca(cliente)
    else:
        obj = Recebimento.objects.create(cliente=cliente, data=HOJE, valor=Decimal("10.00"))
    request = rf.get("/")
    request.user = admin_user
    assert campos <= set(modelo_admin.get_readonly_fields(request, obj))
    # na criação continua dando para digitar
    assert not campos & set(modelo_admin.get_readonly_fields(request, None))


@pytest.mark.parametrize("modelo", [Recebimento, NotaDebito, Caucao])
def test_d11_inlines_sem_editar_valor_nem_incluir(rf, admin_user, modelo):
    request = rf.get("/")
    request.user = admin_user
    inlines = admin.site._registry[modelo].get_inline_instances(request)
    assert inlines
    for inline in inlines:
        assert "valor" in inline.get_readonly_fields(request)
        assert not inline.has_add_permission(request, None)


# ---------- Revisão adversarial ----------


def test_rev_receber_recusa_distribuicao_negativa(usuario_logado, cliente):
    """C07: valor negativo na distribuição inflava a sobra (crédito que não existe)."""
    atrasada = _cobranca(cliente)
    resposta = usuario_logado.post(
        "/financeiro/receber/",
        {
            "cliente": cliente.pk,
            "valor": "100,00",
            "data": HOJE.isoformat(),
            "forma": "pix",
            "sobra_destino": "credito",
            f"aplicar_{atrasada.pk}": "-500,00",
        },
    )
    assert resposta.status_code == 200
    assert not Recebimento.objects.exists()
    assert "negativo" in resposta.content.decode()


def test_rev_caucao_e_nd_recusam_negativo(usuario_logado, alocacao, cliente):
    caucao = services.abrir_caucao(alocacao)
    resposta = usuario_logado.post(
        f"/financeiro/caucoes/{caucao.pk}/",
        {"tipo": "recebimento", "valor": "-10,00", "data": HOJE.isoformat(), "forma": "pix"},
    )
    assert "negativo" in resposta.content.decode()
    resposta = usuario_logado.post(
        "/financeiro/nd/nova/",
        {
            "cliente": cliente.pk,
            "data_emissao": HOJE.isoformat(),
            "token": "",
            "item_descricao": ["Avaria", "Desconto"],
            "item_valor": ["300,00", "-100,00"],
        },
    )
    assert "negativo" in resposta.content.decode()
    assert not NotaDebito.objects.exists()


def _post_com_sessao_antiga(usuario_logado, url, dados, view):
    """Segundo envio que leu a sessão ANTES do primeiro terminar (clique duplo
    com internet lenta): a sessão em memória ainda não tem o token."""
    from django.contrib.auth import get_user_model
    from django.contrib.messages.storage.fallback import FallbackStorage
    from django.contrib.sessions.backends.db import SessionStore
    from django.test import RequestFactory

    chave = usuario_logado.session.session_key
    antiga = SessionStore(chave)
    assert "_auth_user_id" in antiga  # carrega a sessão agora, antes do 1º envio
    usuario_logado.post(url, dados)  # primeiro envio grava
    request = RequestFactory().post(url, dados)
    request.session = antiga
    request.user = get_user_model().objects.get(pk=antiga["_auth_user_id"])
    request._messages = FallbackStorage(request)
    return view(request)


def test_rev_c06_clique_duplo_simultaneo_na_caucao(usuario_logado, alocacao):
    from apps.financeiro.views import caucao_detalhe

    caucao = services.abrir_caucao(alocacao)
    dados = {
        "tipo": "recebimento",
        "valor": "1.500,00",
        "data": HOJE.isoformat(),
        "forma": "pix",
        "token": "simultaneo1",
    }
    url = f"/financeiro/caucoes/{caucao.pk}/"
    _post_com_sessao_antiga(
        usuario_logado, url, dados, lambda r: caucao_detalhe(r, caucao_id=caucao.pk)
    )
    assert caucao.movimentacoes.count() == 1


def test_rev_c06_clique_duplo_simultaneo_na_nd(usuario_logado, cliente):
    from apps.financeiro.views import nd_nova

    dados = {
        "cliente": cliente.pk,
        "data_emissao": HOJE.isoformat(),
        "token": "simultaneo2",
        "item_descricao": "Avaria",
        "item_valor": "300,00",
    }
    _post_com_sessao_antiga(usuario_logado, "/financeiro/nd/nova/", dados, nd_nova)
    assert NotaDebito.objects.count() == 1


def test_rev_d11_item_de_nd_nao_se_apaga_no_admin(rf, admin_user):
    """Apagar item da ND no Admin deixava a cobrança com o valor antigo (D11 caso A)."""
    request = rf.get("/")
    request.user = admin_user
    for inline in admin.site._registry[NotaDebito].get_inline_instances(request):
        assert not inline.has_delete_permission(request, None)


@pytest.mark.parametrize("modelo", [Cobranca, Recebimento, NotaDebito, Caucao])
def test_rev_d11_admin_nao_restaura_versao_antiga(rf, admin_user, modelo):
    """'Reverter' do histórico regravaria valor/status antigos sem refazer a conta."""
    request = rf.get("/")
    request.user = admin_user
    modelo_admin = admin.site._registry[modelo]
    assert modelo_admin.revert_disabled(request)
    from django.core.exceptions import PermissionDenied

    post = rf.post("/")
    post.user = admin_user
    with pytest.raises(PermissionDenied):
        modelo_admin.history_form_view(post, "1", "1")


def test_rev_c01_caucao_da_edicao_recebe_sobra_e_vai_ao_acerto(usuario_logado, alocacao, cliente):
    """Cenário completo do C01: caução posta na edição → sobra vai para ela → acerto."""
    usuario_logado.post(f"/alocacoes/{alocacao.pk}/editar/", _edicao(caucao_valor="1.000,00"))
    services.registrar_recebimento(
        cliente=cliente,
        data=HOJE,
        valor=Decimal("1000.00"),
        forma=Recebimento.Forma.PIX,
        aplicacoes=[],
        sobra_destino="caucao",
    )
    caucao = Caucao.objects.get(alocacao=alocacao)
    assert caucao.saldo == Decimal("1000.00")
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/encerrar/",
        {"data_termino": HOJE.isoformat(), "km_devolucao": "50100", "confirmar_km": "on"},
    )
    assert resposta["Location"] == f"/financeiro/caucoes/{caucao.pk}/"
