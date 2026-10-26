"""Pendências da etapa 15 na frente painel-global (painel, base.html, busca)."""

from datetime import date

import pytest

from apps.frota.models import Veiculo
from apps.manutencao.models import ItemPreventiva, Manutencao
from apps.manutencao.services import preventivas_em_alerta, preventivas_sem_registro


@pytest.fixture
def oleo(db):
    item, _ = ItemPreventiva.objects.get_or_create(
        nome="Troca de óleo e filtro", defaults={"intervalo_km_padrao": 10_000}
    )
    ItemPreventiva.objects.exclude(pk=item.pk).update(ativo=False)  # isola o plano no teste
    return item


def test_painel_lista_preventivas_sem_registro_com_link_para_informar(usuario_logado, oleo):
    """Item nunca registrado e sem KM da compra: o painel pede a última troca."""
    carro = Veiculo.objects.create(placa="SEM1R00", marca_modelo="Gol")
    resposta = usuario_logado.get("/")
    html = resposta.content.decode()
    assert "Sem registro: informe quando foi a última troca" in html
    assert resposta.context["preventivas_sem_registro"] == preventivas_sem_registro()
    link = f"/manutencao/registrar/{carro.pk}/?item={oleo.pk}&ultima=1&volta=/"
    assert link in html
    assert f"/manutencao/preventivas/#veiculo-{carro.pk}" in html


def test_painel_sem_bloco_quando_tudo_tem_registro(usuario_logado, oleo):
    carro = Veiculo.objects.create(placa="COM1R00", marca_modelo="Gol", km_atual=12_000)
    Manutencao.objects.create(
        veiculo=carro, tipo="preventiva", item=oleo, data=date(2026, 1, 1), km=10_000
    )
    resposta = usuario_logado.get("/")
    assert resposta.context["preventivas_sem_registro"] == []
    assert "Sem registro: informe" not in resposta.content.decode()


def test_preventiva_em_alerta_leva_ao_plano_do_carro(usuario_logado, oleo):
    """A placa do alerta abre o plano já no carro (como o hub da frota)."""
    carro = Veiculo.objects.create(
        placa="VEN1C00", marca_modelo="Gol", km_compra=10_000, km_atual=25_000
    )
    resposta = usuario_logado.get("/")
    assert resposta.context["alertas_preventivas"] == preventivas_em_alerta()
    assert resposta.context["alertas_preventivas"][0][0] == carro
    assert f"/manutencao/preventivas/#veiculo-{carro.pk}" in resposta.content.decode()


def test_painel_tem_botao_recebi_para_quem_deve():
    """O 'Recebi' do painel abre a tela de receber com o cliente escolhido."""
    with open("templates/painel.html", encoding="utf-8") as arquivo:
        texto = arquivo.read()
    assert "{% url 'financeiro:receber' %}?cliente={{ d.cliente.pk }}" in texto
    assert "✓ Recebi" in texto


def test_busca_do_topo_explica_o_que_procura(usuario_logado):
    html = usuario_logado.get("/").content.decode()
    assert "Buscar placa ou cliente" not in html
    assert 'placeholder="Placa, nome, CPF, telefone ou nº da multa"' in html


def test_cabecalho_some_na_impressao(usuario_logado):
    html = usuario_logado.get("/").content.decode()
    assert '<header class="bg-marca-preto text-white print:hidden"' in html
    assert "body>header{display:none!important}" in html


def test_sem_registro_longo_fica_fechado_no_celular(usuario_logado, oleo):
    """Frota recém-cadastrada: com mais de 3 carros, o bloco começa fechado."""
    for n in range(4):
        Veiculo.objects.create(placa=f"SRG{n}A00", marca_modelo="Gol")
    html = usuario_logado.get("/").content.decode()
    assert "(4 carros)</summary>" in html
    assert '<details class="mt-4 border-t border-gray-100 pt-3">' in html  # sem "open"


def test_sem_registro_curto_fica_aberto(usuario_logado, oleo):
    Veiculo.objects.create(placa="SRA1A00", marca_modelo="Gol")
    html = usuario_logado.get("/").content.decode()
    assert '<details class="mt-4 border-t border-gray-100 pt-3" open>' in html
    assert "(1 carro)</summary>" in html


def test_alerta_com_zero_dias_nao_mostra_0_dias(usuario_logado, oleo, monkeypatch):
    """faltam_km > 0 mas no ritmo do carro já passou: texto claro, sem '≈ 0 dias'."""
    from apps.manutencao.services import StatusPreventiva

    carro = Veiculo.objects.create(placa="ZER0D00", marca_modelo="Gol", km_atual=19_500)
    status = StatusPreventiva(
        item=oleo,
        intervalo_km=10_000,
        ultima=None,
        km_proximo=20_000,
        faltam_km=500,
        media_km_dia=400.0,
        km_leitura=19_500,
        data_leitura=date(2026, 1, 1),
        hoje=date(2026, 1, 10),
    )
    assert status.dias_restantes == 0
    monkeypatch.setattr(
        "config.views._preventivas_do_painel",
        lambda: {"alertas_preventivas": [(carro, [status])], "preventivas_sem_registro": []},
    )
    html = usuario_logado.get("/").content.decode()
    assert "no ritmo do carro, deve vencer hoje" in html
    assert "≈ 0 dia" not in html
