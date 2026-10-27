"""Etapa 17 (navegador) — multas: botão da FICI com cara de ação, selo de FICI
sem prazo com aviso, seletor de carro com a placa primeiro e alvos de toque."""

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.frota.models import Veiculo
from apps.multas.forms import MultaForm, OrgaoForm
from apps.multas.models import Multa

HOJE = timezone.localdate()


def _multa(**campos):
    veiculo = Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol")
    campos.setdefault("data_infracao", HOJE - timedelta(days=30))
    campos.setdefault("valor", Decimal("100.00"))
    return Multa.objects.create(veiculo=veiculo, **campos)


def test_botao_da_fici_diz_que_e_uma_acao(usuario_logado):
    _multa(fici_prazo=HOJE + timedelta(days=5))
    html = usuario_logado.get("/multas/").content.decode()
    assert "Já indiquei o condutor ✓" in html
    assert ">Condutor indicado<" not in html
    assert "confirm(" in html


def test_fici_sem_prazo_avisa_para_ver_a_notificacao(usuario_logado):
    _multa(fici_prazo=None)
    html = usuario_logado.get("/multas/").content.decode()
    assert "Indicar — prazo não informado (confira na notificação)" in html
    assert "(sem prazo)" not in html


def test_seletor_de_carro_mostra_a_placa_primeiro(db):
    Veiculo.objects.create(placa="ABC1D23", marca_modelo="Onix")
    rotulos = [rotulo for _, rotulo in MultaForm().fields["veiculo"].choices]
    assert "ABC1D23 — Onix" in rotulos
    assert "---------" not in rotulos


def test_orgao_form_sem_tracinhos(db):
    for campo in OrgaoForm().fields.values():
        for _, rotulo in getattr(campo, "choices", []):
            assert rotulo != "---------"


def test_links_do_topo_tem_alvo_de_toque(usuario_logado):
    html = usuario_logado.get("/multas/").content.decode()
    assert 'class="alvo-toque underline">Órgãos' in html
    assert 'class="alvo-toque underline">Gerar ND' in html


def test_selo_sem_prazo_pode_quebrar_linha_no_celular(usuario_logado):
    _multa(fici_prazo=None)
    html = usuario_logado.get("/multas/").content.decode()
    inicio = html.index("Indicar — prazo não informado")
    selo = html[html.rindex("<span", 0, inicio) : inicio]
    assert "whitespace-nowrap" not in selo
    assert "bg-yellow-100" in selo


def test_botao_nova_multa_tem_alvo_de_toque(usuario_logado):
    html = usuario_logado.get("/multas/").content.decode()
    inicio = html.index("+ Nova multa")
    assert "alvo-toque" in html[html.rindex("<a ", 0, inicio) : inicio]
