"""Etapa 17 (navegador), área global: painel, busca, login, seletores, menu e Admin."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from django import forms
from django.contrib import admin
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.campos import FormularioBR
from apps.frota.models import Veiculo
from apps.manutencao.models import Manutencao
from apps.multas.models import Multa
from apps.pessoas.models import Cliente, CondutorAutorizado

RAIZ = Path(__file__).resolve().parents[1]


def _carro(placa, **extra):
    return Veiculo.objects.create(placa=placa, marca_modelo="Gol", km_atual=10_000, **extra)


def _alocar(veiculo, cliente):
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=timezone.localdate() - timedelta(days=20),
        dia_vencimento=0,
        valor_semanal=Decimal("700.00"),
        km_entrega=10_000,
    )


def _na_oficina(veiculo):
    return Manutencao.objects.create(
        veiculo=veiculo,
        tipo=Manutencao.Tipo.choices[0][0],
        data=timezone.localdate(),
        descricao="Funilaria",
        data_entrada=timezone.localdate() - timedelta(days=2),
    )


# ---------- painel: ocupação e "na oficina" ----------


def test_ocupacao_nao_conta_duas_vezes_o_cliente_com_substituto(usuario_logado):
    cliente = Cliente.objects.create(nome="Juliana", cpf_cnpj="111.222.333-44")
    principal = _carro("AAA1A11")
    alocacao = _alocar(principal, cliente)
    substituto = _carro("BBB2B22")
    _carro("CCC3C33")
    _carro("DDD4D44")
    TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=timezone.localdate(),
        km_retirada=10_000,
    )
    principal.refresh_from_db()
    substituto.refresh_from_db()
    assert principal.status == substituto.status == Veiculo.Status.ALOCADO

    resposta = usuario_logado.get(reverse("painel"))

    # 1 cliente com carro (o substituto) em 4 carros = 25%, não 50%
    assert resposta.context["ocupacao"] == 25


def test_card_em_manutencao_mostra_os_alugados_que_estao_na_oficina(usuario_logado):
    cliente = Cliente.objects.create(nome="Juliana", cpf_cnpj="111.222.333-44")
    alugado = _carro("AAA1A11")
    _alocar(alugado, cliente)
    livre = _carro("BBB2B22", status=Veiculo.Status.EM_MANUTENCAO)
    _na_oficina(alugado)
    _na_oficina(alugado)  # duas idas em aberto do mesmo carro contam uma vez
    _na_oficina(livre)

    resposta = usuario_logado.get(reverse("painel"))

    assert resposta.context["na_oficina"] == 2
    assert resposta.context["na_oficina_com_cliente"] == 1
    assert "2 na oficina agora (1 ainda ligado a cliente)" in resposta.content.decode()


def test_card_em_manutencao_sem_alugado_na_oficina_fica_como_antes(usuario_logado):
    livre = _carro("BBB2B22", status=Veiculo.Status.EM_MANUTENCAO)
    _na_oficina(livre)
    resposta = usuario_logado.get(reverse("painel"))
    assert "na oficina agora" not in resposta.content.decode()


# ---------- busca: achou pelo condutor ----------


def test_busca_pelo_condutor_explica_de_quem_e_a_ficha(usuario_logado):
    juliana = Cliente.objects.create(nome="Juliana Souza", cpf_cnpj="111.222.333-44")
    CondutorAutorizado.objects.create(cliente=juliana, nome="Marcos Lima")

    resposta = usuario_logado.get(reverse("buscar"), {"q": "marcos"}, follow=True)

    assert resposta.redirect_chain[-1][0] == reverse("pessoas:detalhe", args=[juliana.pk])
    avisos = [str(m) for m in resposta.context["messages"]]
    assert "Marcos Lima é condutor autorizado de Juliana Souza." in avisos


def test_busca_pelo_nome_do_cliente_nao_mostra_aviso_de_condutor(usuario_logado):
    juliana = Cliente.objects.create(nome="Juliana Souza", cpf_cnpj="111.222.333-44")
    resposta = usuario_logado.get(reverse("buscar"), {"q": "juliana"}, follow=True)
    assert resposta.redirect_chain[-1][0] == reverse("pessoas:detalhe", args=[juliana.pk])
    assert not [m for m in resposta.context["messages"] if "condutor" in str(m)]


# ---------- CSRF no próprio /entrar/ ----------


@pytest.fixture
def cliente_csrf(db):
    return Client(enforce_csrf_checks=True)


def test_csrf_no_login_deslogado_volta_ao_login_com_aviso(cliente_csrf):
    url = reverse("login") + "?next=/frota/"
    resposta = cliente_csrf.post(url, {"username": "x", "password": "y"})
    assert resposta.status_code == 302
    assert resposta["Location"] == url
    pagina = cliente_csrf.get(url)
    assert "Tente entrar de novo" in pagina.content.decode()
    assert "sessão expirou" not in pagina.content.decode().lower()


def test_csrf_no_login_ja_logado_vai_ao_painel(cliente_csrf, django_user_model):
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    cliente_csrf.login(username="dono", password="senha-forte-123")
    resposta = cliente_csrf.post(reverse("login"), {"username": "dono", "password": "x"})
    assert resposta.status_code == 302
    assert resposta["Location"] == reverse("painel")


def test_csrf_fora_do_login_continua_com_a_pagina_de_aviso(cliente_csrf, django_user_model):
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    cliente_csrf.login(username="dono", password="senha-forte-123")
    resposta = cliente_csrf.post(reverse("logout"))
    assert resposta.status_code == 403


# ---------- seletores de carro com a placa primeiro ----------


class _MultaVeiculoForm(FormularioBR, forms.ModelForm):
    class Meta:
        model = Multa
        fields = ["veiculo"]


def test_seletor_de_carro_mostra_a_placa_primeiro(db):
    carro = _carro("ABC1D23")
    campo = _MultaVeiculoForm().fields["veiculo"]
    assert campo.label_from_instance(carro) == "ABC1D23 — Gol"


def test_formularios_de_multa_e_sinistro_mostram_a_placa_primeiro(usuario_logado):
    _carro("ABC1D23")
    for nome in ("multas:nova", "sinistros:novo"):
        html = usuario_logado.get(reverse(nome)).content.decode()
        assert "ABC1D23 — Gol" in html, nome
        assert "Gol — ABC1D23" not in html, nome


# ---------- cabeçalho do celular e "+ Lançar" ----------


def test_cabecalho_do_celular_tem_lupa_e_menu_com_44px(usuario_logado):
    html = usuario_logado.get(reverse("painel")).content.decode()
    assert f'href="{reverse("buscar")}" data-alvo-livre' in html
    assert 'class="flex h-11 w-11 items-center justify-center rounded text-xl' in html
    assert 'aria-label="Abrir menu">☰' in html


def test_menu_lancar_tem_devolucao_de_carro(usuario_logado):
    html = usuario_logado.get(reverse("painel")).content.decode()
    menu = html.split('id="menu-lancar"', 1)[1].split("</nav>", 1)[0]
    assert reverse("alocacoes:lista") in menu
    assert "Devolução de carro" in menu


def test_painel_usa_alvo_de_toque_nos_links_de_acao():
    painel = (RAIZ / "templates" / "painel.html").read_text()
    base = (RAIZ / "templates" / "base.html").read_text()
    assert ".alvo-toque{" in base
    assert 'class="alvo-toque font-medium underline">registrar devolução' in painel
    assert painel.count("alvo-toque") >= 4


# ---------- primeiro erro do formulário ----------


def test_ux_js_leva_ao_primeiro_campo_com_erro():
    js = (RAIZ / "static" / "js" / "ux.js").read_text()
    assert "mostrarPrimeiroErro(raiz)" in js
    assert '[aria-invalid="true"]' in js
    assert 'scrollIntoView({ block: "center" })' in js
    resumo = (RAIZ / "templates" / "_erros_form.html").read_text()
    assert "data-resumo-erros" in resumo


def test_campo_com_erro_sai_marcado_para_o_js(usuario_logado):
    html = usuario_logado.post(reverse("multas:nova"), {}).content.decode()
    assert 'aria-invalid="true"' in html


# ---------- Admin ----------


def test_acao_do_admin_sem_tracos(admin_client):
    Cliente.objects.create(
        nome="Juliana", cpf_cnpj="111.222.333-44"
    )  # lista vazia não mostra ações
    html = admin_client.get(reverse("admin:pessoas_cliente_changelist")).content.decode()
    assert "— escolha a ação —" in html
    assert ">---------<" not in html


def test_get_action_choices_continua_aceitando_lista_propria(rf, admin_user):
    modelo_admin = admin.site._registry[Cliente]
    pedido = rf.get("/")
    pedido.user = admin_user
    escolhas = modelo_admin.get_action_choices(pedido, [("", "x")])
    assert escolhas[0] == ("", "x")
