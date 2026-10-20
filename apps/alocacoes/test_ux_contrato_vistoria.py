"""UX do contrato e da vistoria (proposta #17): entrega/devolução sem confusão."""

from datetime import date
from decimal import Decimal

import pytest

from apps.alocacoes import checklist
from apps.alocacoes.models import Alocacao, Vistoria
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente, CondutorAutorizado


@pytest.fixture
def alocacao(db):
    veiculo = Veiculo.objects.create(placa="QXQ6C10", marca_modelo="VW/Gol 1.0")
    cliente = Cliente.objects.create(
        nome="Arlen Souza", cpf_cnpj="111.222.333-44", telefone="(31) 99999-0000"
    )
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=50_000,
    )


def test_rotulos_do_tipo_falam_de_entrega_e_devolucao():
    assert Vistoria.Tipo.ENTRADA.label == "Entrega ao motorista"
    assert Vistoria.Tipo.SAIDA.label == "Devolução pelo motorista"
    # o valor gravado não mudou
    assert Vistoria.Tipo.ENTRADA.value == "entrada" and Vistoria.Tipo.SAIDA.value == "saida"


def test_leitura_da_foto_usa_os_mesmos_nomes():
    assert "Entrega ao motorista" in checklist.INSTRUCAO
    assert "Devolução pelo motorista" in checklist.INSTRUCAO
    for letra, nome in checklist.LEGENDA_AVARIAS:
        assert f"{letra} = {nome}" in checklist.INSTRUCAO


def test_checklist_impresso_tem_tipo_claro_desenho_e_assinaturas(usuario_logado, alocacao):
    html = usuario_logado.get(f"/alocacoes/{alocacao.pk}/vistoria/imprimir/").content.decode()
    assert "Entrega ao motorista" in html and "Devolução pelo motorista" in html
    assert "Entrada (entrega)" not in html and "Saída (devolução)" not in html
    assert "<svg" in html and "Visto de cima" in html
    # a legenda impressa é a mesma que a leitura da foto conhece
    legenda = " · ".join(f"{letra} = {nome}" for letra, nome in checklist.LEGENDA_AVARIAS)
    assert legenda in html
    assert "111.222.333-44" in html and "(31) 99999-0000" in html
    assert "Assinatura da locadora" in html and "Assinatura do motorista" in html
    assert "2 vias" in html
    # uma lista só: cartões no celular, 2 colunas no computador/papel (cabe numa folha A4)
    for item in checklist.ITENS_CHECKLIST:
        assert html.count(item) == 1
    assert "print:columns-2" in html
    # quadrinho desenhado (o caractere ☐ sai minúsculo no papel) e nenhum comentário vazando
    assert "☐" not in html and html.count('class="quadrinho"') >= 2 * len(checklist.ITENS_CHECKLIST)
    assert "{#" not in html and "#}" not in html


def test_formulario_de_vistoria_tem_cancelar_e_explica_o_tipo(usuario_logado, alocacao):
    html = usuario_logado.get(f"/alocacoes/{alocacao.pk}/vistoria/nova/").content.decode()
    assert "Cancelar" in html
    assert "Entrega ao motorista" in html and "o carro sai da locadora" in html


def test_contrato_tem_data_do_inicio_testemunhas_e_condutores(usuario_logado, alocacao):
    CondutorAutorizado.objects.create(
        nome="Bia Lima", cpf="555.666.777-88", cnh_numero="98765", cliente=alocacao.cliente
    )
    html = usuario_logado.get(f"/alocacoes/{alocacao.pk}/contrato/").content.decode()
    assert "01 de julho de 2026" in html  # data de início, não a da impressão
    assert html.count('class="testemunha') == 2
    assert "Assinatura da 1ª testemunha" in html and "Assinatura da 2ª testemunha" in html
    assert "Bia Lima" in html and "555.666.777-88" in html and "98765" in html
    assert "EMPRESA_" not in html
    assert "podem ser preenchidos à caneta" in html


def test_contrato_sem_cnpj_mostra_mascara_certa(usuario_logado, alocacao, settings):
    settings.EMPRESA_CNPJ = ""
    html = usuario_logado.get(f"/alocacoes/{alocacao.pk}/contrato/").content.decode()
    assert "__.___.___/____-__" in html and "___.___.___/____-__" not in html
    assert "Somente o(a) LOCATÁRIO(A)" in html


def test_contrato_lista_so_condutores_deste_cliente(usuario_logado, alocacao):
    outro = Cliente.objects.create(nome="Outro Cliente", cpf_cnpj="999.888.777-66")
    CondutorAutorizado.objects.create(nome="Condutor Alheio", cliente=outro)
    CondutorAutorizado.objects.create(nome="Condutor Solto")
    html = usuario_logado.get(f"/alocacoes/{alocacao.pk}/contrato/").content.decode()
    assert "Condutor Alheio" not in html and "Condutor Solto" not in html
    assert "Somente o(a) LOCATÁRIO(A)" in html


def test_salvar_vistoria_de_entrega_usa_o_nome_novo(usuario_logado, alocacao):
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/vistoria/nova/",
        {"tipo": "entrada", "data": "2026-07-01", "km": "50000", "combustivel": "cheio"},
        follow=True,
    )
    vistoria = Vistoria.objects.get(alocacao=alocacao)
    assert vistoria.tipo == Vistoria.Tipo.ENTRADA
    assert "Vistoria de entrega ao motorista registrada" in resposta.content.decode()
    # o histórico da vistoria mostra o nome novo, não "Entrada"
    html = usuario_logado.get(f"/alocacoes/{alocacao.pk}/vistoria/nova/").content.decode()
    assert "Entrega ao motorista — 01/07/2026" in html


def test_vistoria_de_alocacao_inexistente_da_404(usuario_logado, db):
    assert usuario_logado.get("/alocacoes/999999/vistoria/imprimir/").status_code == 404
    assert usuario_logado.get("/alocacoes/999999/contrato/").status_code == 404


def test_paginas_exigem_login(client, alocacao):
    for url in (
        f"/alocacoes/{alocacao.pk}/contrato/",
        f"/alocacoes/{alocacao.pk}/vistoria/imprimir/",
        f"/alocacoes/{alocacao.pk}/vistoria/nova/",
    ):
        assert client.get(url).status_code == 302
