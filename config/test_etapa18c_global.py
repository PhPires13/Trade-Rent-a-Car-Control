"""Etapa 18c, área global: sobras da etapa 18 e refatoração (painel, busca, datas, deploy).

(O config/test_etapa18c.py é o do termo único "Caução retida"/"Crédito do cliente".)
"""

import gzip
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from django import forms
from django.core.exceptions import ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps import campos as modulo_campos
from apps.alocacoes.models import Alocacao
from apps.campos import FormularioBR
from apps.financeiro import services as financeiro
from apps.financeiro.devedores import Devedor
from apps.financeiro.models import Caucao, Cobranca
from apps.frota.models import Veiculo
from apps.manutencao.models import Manutencao
from apps.pessoas.models import Cliente
from apps.sinistros.models import AuxilioMotorista, Sinistro
from config.test_conferencia_18b import _montar

RAIZ = Path(__file__).resolve().parents[1]
BASE = (RAIZ / "templates" / "base.html").read_text()
HOJE = timezone.localdate()


# ---------- painel: "·" solto antes da placa quando o cliente tem crédito ----------


def _painel_com_devedor(usuario_logado, monkeypatch, credito):
    cliente = Cliente.objects.create(nome="Duda Devedora", cpf_cnpj="123.456.789-09")
    devedor = Devedor(
        cliente=cliente,
        atrasado=Decimal("700.00"),
        mais_antigo=HOJE - timedelta(days=3),
        dias_atraso=3,
        placas=["ABC1D23"],
        credito=credito,
    )
    resumo = {
        "hoje_linhas": [devedor],
        "hoje_mais": 0,
        "hoje_qtde": 1,
        "hoje_total": devedor.total,
        "hoje_pagos": [],
    }
    monkeypatch.setattr("config.views.resumo_do_dia", lambda hoje: resumo)
    return usuario_logado.get("/").content.decode()


@pytest.mark.django_db
def test_placa_sem_ponto_solto_quando_o_cliente_tem_credito(usuario_logado, monkeypatch):
    html = _painel_com_devedor(usuario_logado, monkeypatch, Decimal("50.00"))
    assert "de crédito" in html and "ABC1D23" in html
    # o crédito fecha a linha (bloco): a placa abre a linha seguinte, sem "·" na frente
    assert "· ABC1D23" not in html


@pytest.mark.django_db
def test_placa_continua_com_ponto_sem_credito(usuario_logado, monkeypatch):
    html = _painel_com_devedor(usuario_logado, monkeypatch, Decimal("0.00"))
    assert "· ABC1D23" in html


# ---------- busca global: "nº do AIT" ----------


def test_busca_fala_em_numero_do_ait_e_nao_em_numero_da_multa():
    for arquivo in (RAIZ / "templates").rglob("*.html"):
        assert "nº da multa" not in arquivo.read_text(encoding="utf-8"), arquivo.name
    assert BASE.count("nº do AIT") == 4  # rótulo e dica, no computador e no celular


# ---------- <summary> com alvo de toque de 44px ----------


def test_summary_tem_alvo_de_toque_de_44px_no_celular():
    celular = BASE.split("@media (max-width:639px){", 1)[1].split("@media print", 1)[0]
    assert ":where(summary){min-height:44px}" in celular
    # sem peso: o summary com altura/padding próprio (flex, min-h-11, py-3) fica como está
    assert ":where(summary:not(.flex)){padding-block:.625rem}" in celular


# ---------- painel: caução abaixo do combinado ----------


def _contrato_com_caucao(placa, nome, combinado="1000.00"):
    carro = Veiculo.objects.create(placa=placa, marca_modelo="Gol", km_atual=10_000)
    cliente = Cliente.objects.create(nome=nome, cpf_cnpj=f"{placa[-3:]}.222.333-44")
    alocacao = Alocacao.objects.create(
        veiculo=carro,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=20),
        valor_semanal=Decimal("700.00"),
        km_entrega=10_000,
        caucao_valor=Decimal(combinado),
    )
    caucao = financeiro.abrir_caucao(alocacao, Decimal(combinado), HOJE - timedelta(days=20), "pix")
    return alocacao, caucao


def _usar_caucao(alocacao, caucao, valor):
    cobranca = Cobranca.objects.create(
        cliente=alocacao.cliente,
        alocacao=alocacao,
        origem=Cobranca.Origem.ALUGUEL,
        descricao="Aluguel semanal",
        valor=Decimal("700.00"),
        vencimento=HOJE - timedelta(days=2),
    )
    financeiro.descontar_da_caucao(caucao, cobranca, Decimal(valor), HOJE)


@pytest.mark.django_db
def test_painel_avisa_caucao_usada_abaixo_do_combinado(usuario_logado):
    alocacao, caucao = _contrato_com_caucao("CAU1A01", "Carla Caução")
    _usar_caucao(alocacao, caucao, "700.00")
    _contrato_com_caucao("CAU1A02", "Caio Completo")  # caução cheia: não aparece

    resposta = usuario_logado.get("/")
    html = resposta.content.decode()

    assert [c.pk for c in resposta.context["caucoes_a_repor"]] == [caucao.pk]
    assert "Caução abaixo do combinado" in html
    assert f"/financeiro/caucoes/{caucao.pk}/" in html
    assert "falta repor R$ 700,00" in html
    assert "Caio Completo" not in html.split("data-caucao-a-repor", 1)[1].split("</ul>", 1)[0]


@pytest.mark.django_db
def test_painel_sem_caucao_a_repor_nao_mostra_o_bloco(usuario_logado):
    _contrato_com_caucao("CAU2A01", "Caio Completo")
    alocacao, caucao = _contrato_com_caucao("CAU2A02", "Encerrado Usado")
    _usar_caucao(alocacao, caucao, "300.00")
    # contrato encerrado: a caução usada vai para o acerto, não para "repor"
    Alocacao.objects.filter(pk=alocacao.pk).update(status=Alocacao.Status.ENCERRADA)

    html = usuario_logado.get("/").content.decode()
    assert "Caução abaixo do combinado" not in html


@pytest.mark.django_db
def test_caucao_a_repor_e_a_mesma_conta_da_tela_da_caucao(usuario_logado):
    """Depositou 500 de 1.000 e 200 pagaram cobrança: repor 200 (os 500 são "falta depositar")."""
    alocacao, caucao = _contrato_com_caucao("CAU3A01", "Paula Parcial")
    caucao.movimentacoes.update(valor=Decimal("500.00"))
    _usar_caucao(alocacao, caucao, "200.00")

    assert Caucao.objects.get(pk=caucao.pk).a_repor == Decimal("200.00")
    painel = usuario_logado.get("/").content.decode()
    tela = usuario_logado.get(f"/financeiro/caucoes/{caucao.pk}/").content.decode()
    assert "falta repor R$ 200,00" in painel
    assert "falta repor R$ 200,00" in tela


def _consultas(cliente_http, url):
    # o registro de consultas guarda só as 9.000 últimas: a montagem dos dados enche
    connection.queries_log.clear()
    with CaptureQueriesContext(connection) as consultas:
        resposta = cliente_http.get(url)
    assert resposta.status_code == 200
    return len(consultas.captured_queries)


@pytest.mark.django_db
def test_aviso_de_caucao_nao_consulta_linha_a_linha(usuario_logado):
    for i in range(2):
        alocacao, caucao = _contrato_com_caucao(f"CAU4A0{i}", f"Cliente {i}")
        _usar_caucao(alocacao, caucao, "100.00")
    poucas = _consultas(usuario_logado, "/")
    for i in range(2, 12):
        alocacao, caucao = _contrato_com_caucao(f"CAU4A{i:02d}", f"Cliente {i}")
        _usar_caucao(alocacao, caucao, "100.00")
    assert _consultas(usuario_logado, "/") <= poucas
    assert usuario_logado.get("/").content.decode().count("falta repor R$ 100,00") == 12


# ---------- data "dd/mm" sem ano perto da virada do ano ----------


def _agora_utc(monkeypatch, momento):
    """Relógio do servidor (UTC); o "hoje" do sistema é o de Brasília."""
    monkeypatch.setattr(timezone, "now", lambda: momento)


@pytest.mark.parametrize(
    ("digitado", "esperado"),
    [
        ("31/12", date(2026, 12, 31)),  # hoje mesmo
        ("30/12", date(2026, 12, 30)),  # ontem
        ("01/01", date(2027, 1, 1)),  # amanhã
        ("02/01", date(2027, 1, 2)),
    ],
)
def test_virada_usa_o_dia_de_brasilia_e_nao_o_do_servidor(monkeypatch, digitado, esperado):
    # 01/01/2027 01:30 no servidor (UTC) ainda é 31/12/2026 22:30 em Brasília
    _agora_utc(monkeypatch, datetime(2027, 1, 1, 1, 30, tzinfo=UTC))
    assert timezone.localdate() == date(2026, 12, 31)
    assert modulo_campos.ler_data(digitado) == esperado


@pytest.mark.parametrize(
    ("hoje", "digitado", "esperado"),
    [
        (date(2027, 1, 1), "31/12", date(2026, 12, 31)),
        (date(2027, 1, 1), "01/01", date(2027, 1, 1)),
        (date(2027, 1, 15), "20/12", date(2026, 12, 20)),
        (date(2026, 12, 15), "10/01", date(2027, 1, 10)),
        # ano bissexto logo depois da virada: 29/02 vale para o ano que vem
        (date(2027, 12, 31), "29/02", date(2028, 2, 29)),
        (date(2028, 1, 2), "29/02", date(2028, 2, 29)),
    ],
)
def test_dd_mm_perto_da_virada(monkeypatch, hoje, digitado, esperado):
    monkeypatch.setattr(modulo_campos.timezone, "localdate", lambda: hoje)
    assert modulo_campos.ler_data(digitado) == esperado


def test_29_de_fevereiro_longe_do_bissexto_continua_recusado(monkeypatch):
    monkeypatch.setattr(modulo_campos.timezone, "localdate", lambda: date(2029, 1, 1))
    with pytest.raises(ValidationError, match="inválida"):
        modulo_campos.ler_data("29/02")


class _FormData(FormularioBR, forms.Form):
    quando = forms.DateField()


def test_formulario_faz_a_mesma_conta_na_virada(monkeypatch):
    _agora_utc(monkeypatch, datetime(2027, 1, 1, 1, 30, tzinfo=UTC))
    form = _FormData(data={"quando": "31/12"})
    assert form.is_valid(), form.errors
    assert form.cleaned_data["quando"] == date(2026, 12, 31)


# ---------- HSTS: subdomínios e preload pela variável de ambiente ----------


def _settings_de_producao(**variaveis):
    codigo = (
        "import config.settings as s;"
        "print(s.SECURE_HSTS_SECONDS, s.SECURE_HSTS_INCLUDE_SUBDOMAINS, s.SECURE_HSTS_PRELOAD)"
    )
    ambiente = {
        **os.environ,
        "DEBUG": "False",
        "SECRET_KEY": "chave-descartavel-somente-para-testes",
        **variaveis,
    }
    for nome in ("SECURE_HSTS_INCLUDE_SUBDOMAINS", "SECURE_HSTS_PRELOAD"):
        if nome not in variaveis:
            ambiente.pop(nome, None)
    saida = subprocess.run(
        [sys.executable, "-c", codigo], cwd=RAIZ, env=ambiente, capture_output=True, text=True
    )
    assert saida.returncode == 0, saida.stderr
    return saida.stdout.split()


def test_hsts_sem_subdominios_e_sem_preload_por_padrao():
    assert _settings_de_producao() == ["2592000", "False", "False"]


def test_hsts_subdominios_e_preload_ligam_pela_variavel():
    assert _settings_de_producao(
        SECURE_HSTS_INCLUDE_SUBDOMAINS="True", SECURE_HSTS_PRELOAD="True"
    ) == ["2592000", "True", "True"]


def test_env_example_explica_as_variaveis_do_hsts():
    exemplo = (RAIZ / ".env.example").read_text()
    assert "SECURE_HSTS_INCLUDE_SUBDOMAINS" in exemplo
    assert "SECURE_HSTS_PRELOAD" in exemplo


def test_etapas_tem_a_nota_de_deploy_do_contenttypes_e_permissoes():
    etapas = (RAIZ / "ETAPAS.md").read_text()
    mvp = etapas.split("## 🎉 MVP completo", 1)[1]
    assert "remove_stale_contenttypes" in mvp
    assert "permissões" in mvp


# ---------- refatoração: painel com todos os blocos não cresce com os dados ----------


def _com_tudo(inicio, quantidade):
    """A operação de test_conferencia_18b e mais o que liga os ramos caros do painel:
    colisão com proteção acionada e 20 dias de oficina (auxílio), caução usada abaixo
    do combinado, licenciamento vencendo e multa com FICI pendente."""
    _montar(inicio, quantidade)
    for alocacao in Alocacao.objects.filter(caucao_valor__isnull=True).select_related("veiculo"):
        alocacao.caucao_valor = Decimal("1000.00")
        alocacao.save(update_fields=["caucao_valor"])
        Veiculo.objects.filter(pk=alocacao.veiculo_id).update(
            licenciamento_vencimento=HOJE + timedelta(days=10)
        )
        cobranca = alocacao.cobrancas.filter(status=Cobranca.Status.ATRASADO).first()
        financeiro.descontar_da_caucao(alocacao.caucao, cobranca, Decimal("50.00"), HOJE)
        sinistro = Sinistro.objects.create(
            veiculo=alocacao.veiculo,
            cliente=alocacao.cliente,
            data=HOJE - timedelta(days=20),
            envolvido="associado",
            tipo="colisao",
            acionou_protecao=True,
        )
        Manutencao.objects.create(
            veiculo=alocacao.veiculo,
            tipo="corretiva",
            data=HOJE - timedelta(days=20),
            descricao="Funilaria",
            data_entrada=HOJE - timedelta(days=20),
            data_saida=HOJE - timedelta(days=5),
            sinistro=sinistro,
        )
        if alocacao.pk % 2:  # metade já pediu o auxílio
            AuxilioMotorista.objects.create(sinistro=sinistro, status="solicitado")


@pytest.mark.django_db
def test_painel_com_todos_os_blocos_nao_cresce_com_a_frota(usuario_logado):
    _com_tudo(0, 4)
    poucos = _consultas(usuario_logado, "/")
    _com_tudo(4, 16)
    resposta = usuario_logado.get("/")
    contexto = resposta.context
    # os ramos caros estão mesmo ligados
    assert contexto["auxilios_disponiveis"] and contexto["caucoes_a_repor"]
    assert contexto["fici_em_alerta"] and contexto["vigencias"]
    assert contexto["trocas_em_andamento"] and contexto["hoje_linhas"]
    assert _consultas(usuario_logado, "/") == poucos


@pytest.mark.django_db
def test_auxilio_so_para_quem_ainda_nao_pediu(usuario_logado):
    _com_tudo(0, 4)
    sem_auxilio = set(
        Sinistro.objects.filter(acionou_protecao=True, auxilios__isnull=True).values_list(
            "pk", flat=True
        )
    )
    contexto = usuario_logado.get("/").context
    assert {s.pk for s in contexto["auxilios_disponiveis"]} == sem_auxilio
    assert sem_auxilio and Sinistro.objects.filter(acionou_protecao=True).count() == 4


# ---------- refatoração: páginas comprimidas (gzip) ----------


@pytest.mark.django_db
def test_pagina_html_vai_comprimida_e_igual(usuario_logado):
    _montar(0, 5)
    simples = usuario_logado.get("/alocacoes/")
    comprimida = usuario_logado.get("/alocacoes/", HTTP_ACCEPT_ENCODING="gzip")
    assert "Content-Encoding" not in simples
    assert comprimida["Content-Encoding"] == "gzip"
    assert "Accept-Encoding" in comprimida["Vary"]
    assert len(comprimida.content) < len(simples.content) * 0.25
    original = gzip.decompress(comprimida.content).decode()
    # o token CSRF muda a cada resposta (mascarado): compara sem ele
    sem_token = [linha for linha in original.splitlines() if "csrf" not in linha.lower()]
    esperado = [
        linha for linha in simples.content.decode().splitlines() if "csrf" not in linha.lower()
    ]
    assert sem_token == esperado


@pytest.mark.django_db
def test_foto_e_planilha_nao_sao_recomprimidas(usuario_logado, settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    (tmp_path / "foto.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 2000)
    foto = usuario_logado.get("/midia/foto.jpg", HTTP_ACCEPT_ENCODING="gzip")
    assert foto.status_code == 200 and "Content-Encoding" not in foto
    assert b"".join(foto.streaming_content).startswith(b"\xff\xd8\xff")
    saude = usuario_logado.get("/saude/", HTTP_ACCEPT_ENCODING="gzip")
    assert saude.content == b"ok" and "Content-Encoding" not in saude


# ---------- notebook de 1024 a 1279px: menu do topo, sem a barra de celular ----------


def _classes(html, marcador):
    """Classes da tag que contém `marcador` (ex.: 'aria-label="Seções"')."""
    fim = html.index(marcador)
    inicio = html.rindex("<", 0, fim)
    return re.search(r'class="([^"]*)"', html[inicio : html.index(">", fim)]).group(1).split()


def test_notebook_de_1100px_ja_tem_o_menu_do_topo_e_nao_a_barra_de_celular(usuario_logado):
    html = usuario_logado.get("/").content.decode()
    # menu completo a partir de lg (1024px) — antes só em xl (1280px)
    assert "lg:flex" in _classes(html, 'aria-label="Seções"')
    assert "lg:hidden" in _classes(html, 'aria-label="Abrir menu"')
    assert "lg:hidden" in _classes(html, 'id="menu-movel"')
    # a barra fixa de baixo (Painel/Receber/KM/+Lançar) some no notebook
    assert "lg:hidden" in _classes(html, 'aria-label="Atalhos"')
    main = _classes(html, 'id="conteudo"')
    assert "pb-28" in main and "lg:pb-6" in main
    assert "xl:hidden" not in " ".join(
        _classes(html, 'aria-label="Atalhos"') + _classes(html, 'aria-label="Abrir menu"')
    )


def test_de_1024_a_1279px_a_busca_vira_lupa_para_o_menu_caber(usuario_logado):
    html = usuario_logado.get("/").content.decode()
    campo = _classes(html, 'role="search"')
    assert {"sm:block", "lg:hidden", "xl:block"} <= set(campo)
    lupa = _classes(html, 'aria-label="Buscar placa, nome, CPF, telefone ou nº do AIT"\n')
    assert {"sm:hidden", "lg:flex", "xl:hidden"} <= set(lupa)
    # "Rent a Car" e o nome da conta só voltam com espaço sobrando (xl)
    assert 'sm:inline lg:hidden xl:inline">Rent a Car' in html
    assert re.search(r'md:inline lg:hidden xl:inline">[^<]+</span>', html)


# ---------- ux.js: links dentro de <details> fechado não contam como vizinhos ----------


def _rodar_no_node(codigo):
    if shutil.which("node") is None:
        pytest.skip("node não instalado")
    saida = subprocess.run(
        ["node", "-e", codigo], capture_output=True, text=True, timeout=30, check=False
    )
    assert saida.returncode == 0, saida.stderr
    return saida.stdout.strip()


def test_ux_js_ignora_o_conteudo_de_details_fechado():
    ux = (RAIZ / "static" / "js" / "ux.js").read_text()
    funcao = re.search(
        r"function escondidoEmDetailsFechado\(el\) \{.*?\n  \}\n", ux, re.DOTALL
    ).group(0)
    # o filtro roda antes de medir o vizinho, junto do getClientRects
    trecho = ux[ux.index("function limitarAlvosDeToque") :]
    assert "if (escondidoEmDetailsFechado(el)) return;" in trecho[: trecho.index("ajustes")]
    # DOM mínimo: details (aberto ou não) > summary + links; details fechado dentro de aberto
    dom = r"""
    function no(tag, pai, attrs) {
      const n = { tag: tag, pai: pai || null, filhos: [], attrs: attrs || {} };
      if (pai) pai.filhos.push(n);
      n.parentElement = n.pai;
      n.contains = function (o) {
        for (let x = o; x; x = x.pai) if (x === n) return true;
        return false;
      };
      n.closest = function (sel) {
        for (let x = n; x; x = x.pai) if (x.tag === "details" && !("open" in x.attrs)) return x;
        return null;
      };
      n.querySelector = function () {
        return n.filhos.find(function (f) { return f.tag === "summary"; }) || null;
      };
      return n;
    }
    const main = no("main");
    const fechado = no("details", main);
    const resumo = no("summary", fechado);
    const linkNoResumo = no("a", resumo);
    const escondido = no("a", fechado);
    const aberto = no("details", main, { open: "" });
    const visivel = no("a", aberto);
    const fechadoDentro = no("details", aberto);
    const resumoDentro = no("summary", fechadoDentro);
    const escondidoDentro = no("a", fechadoDentro);
    const solto = no("a", main);
    """
    resultado = _rodar_no_node(
        dom
        + funcao
        + "console.log(JSON.stringify([solto, resumo, linkNoResumo, escondido, visivel,"
        " resumoDentro, escondidoDentro].map(escondidoEmDetailsFechado)));"
    )
    assert json.loads(resultado) == [False, False, False, True, False, False, True]
