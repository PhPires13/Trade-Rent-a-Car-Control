"""Etapa 18c: achados do navegador na ficha e na lista de clientes."""

import re
from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.financeiro.models import Caucao, Cobranca, MovimentacaoCaucao
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente

HOJE = timezone.localdate()


def _alocacao_com_caucao(placa="SEF1A01", cpf="529.982.247-25"):
    veiculo = Veiculo.objects.create(placa=placa, marca_modelo="Onix", km_atual=50_000)
    cliente = Cliente.objects.create(nome="Luciana Teste", cpf_cnpj=cpf)
    inicio = HOJE - timedelta(days=3)
    alocacao = Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=inicio,
        valor_semanal=Decimal("650.00"),
        dia_vencimento=inicio.weekday(),
        km_entrega=50_000,
        caucao_valor=Decimal("1000.00"),
    )
    caucao, _ = Caucao.objects.get_or_create(alocacao=alocacao)
    MovimentacaoCaucao.objects.create(
        caucao=caucao, tipo="recebimento", valor=Decimal("1000.00"), data=inicio, forma="pix"
    )
    return alocacao, caucao


def _usar_caucao(alocacao, caucao, valor="200.00"):
    nd = Cobranca.objects.create(
        cliente=alocacao.cliente,
        alocacao=alocacao,
        origem=Cobranca.Origem.ALUGUEL,
        descricao="ND 004",
        valor=Decimal(valor),
        vencimento=HOJE,
    )
    MovimentacaoCaucao.objects.create(
        caucao=caucao, tipo="desconto", valor=Decimal(valor), data=HOJE, cobranca=nd
    )


def _texto(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def test_ficha_mostra_falta_repor_quando_a_caucao_pagou_cobranca(usuario_logado):
    alocacao, caucao = _alocacao_com_caucao()
    _usar_caucao(alocacao, caucao)
    html = usuario_logado.get(reverse("pessoas:detalhe", args=[alocacao.cliente_id])).content
    html = html.decode()
    texto = _texto(html)
    assert "saldo R$ 800,00, falta repor R$ 200,00" in texto
    # link para a caução, que já sugere o reforço
    assert reverse("financeiro:caucao_detalhe", args=[caucao.pk]) in html
    assert "Repor a caução" in texto


def test_ficha_caucao_completa_sem_uso_continua_depositada(usuario_logado):
    alocacao, caucao = _alocacao_com_caucao()
    html = usuario_logado.get(reverse("pessoas:detalhe", args=[alocacao.cliente_id])).content
    texto = _texto(html.decode())
    assert "— depositada" in texto
    assert "falta repor" not in texto
    assert "Repor a caução" not in texto


def test_lista_de_clientes_links_do_card_com_alvo_de_toque(usuario_logado):
    _alocacao_com_caucao()
    Cliente.objects.create(nome="Sem Carro", cpf_cnpj="111.444.777-35")
    html = usuario_logado.get(reverse("pessoas:lista")).content.decode()
    placa = re.search(r'<a href="[^"]*"\s+class="([^"]*)">SEF1A01</a>', html)
    assert placa and "min-h-11" in placa.group(1)
    alocar = re.search(r'class="([^"]*)">alocar um carro</a>', html)
    assert alocar and "min-h-11" in alocar.group(1)
