"""UX global (base.html + static/js/ux.js): barra de atalhos no celular, campos de 16px,
alvos de toque, contraste, "(opcional)", dicas "?" e "Salvando…" no envio."""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.staticfiles import finders
from django.urls import reverse

TEMPLATES = Path(settings.BASE_DIR) / "templates"


@pytest.fixture
def ux_js():
    caminho = finders.find("js/ux.js")
    assert caminho, "static/js/ux.js precisa existir e ser achado pelo collectstatic"
    return Path(caminho).read_text(encoding="utf-8")


def _barra(html):
    """Só o trecho da barra fixa de atalhos."""
    inicio = html.index('aria-label="Atalhos"')
    return html[inicio : html.index("</nav>", inicio)]


def test_barra_de_atalhos_no_celular(usuario_logado):
    html = usuario_logado.get("/").content.decode()
    barra = _barra(html)
    for destino in ("painel", "financeiro:receber", "km:lista"):
        assert f'href="{reverse(destino)}"' in barra
    assert "+ Lançar" in barra
    # o "+ Lançar" abre as tarefas frequentes
    for destino in (
        "manutencao:preventivas",
        "multas:nova",
        "sinistros:novo",
        "alocacoes:nova",
    ):
        assert f'href="{reverse(destino)}"' in barra
    assert "Pagamento recebido" in barra
    # só no celular/tablet, fixa embaixo, e fora da impressão
    assert re.search(r'<nav class="[^"]*\bfixed\b[^"]*\bbottom-0\b[^"]*\bxl:hidden\b', html)
    assert "print:hidden" in html[html.rindex("<nav", 0, html.index('aria-label="Atalhos"')) :]


def test_conteudo_nao_fica_atras_da_barra(usuario_logado):
    html = usuario_logado.get("/").content.decode()
    main = re.search(r'<main [^>]*class="([^"]*)"', html).group(1)
    assert "pb-28" in main and "xl:pb-6" in main


def test_sem_login_nao_tem_barra(client):
    html = client.get(reverse("login")).content.decode()
    assert 'aria-label="Atalhos"' not in html
    assert "pb-28" not in html


_LINK = r'<a href="{}"[^>]*class="([^"]*)"'


def test_barra_destaca_onde_a_pessoa_esta(usuario_logado):
    barra = _barra(usuario_logado.get(reverse("km:lista")).content.decode())
    km = re.search(_LINK.format(reverse("km:lista")), barra).group(1)
    painel = re.search(_LINK.format(reverse("painel")), barra).group(1)
    assert "text-marca-amarelo" in km
    assert "text-marca-amarelo" not in painel


def test_menu_do_topo_continua_com_um_so_destaque(usuario_logado):
    # a barra de baixo destaca só pela cor; aria-current segue único (menu do topo)
    assert usuario_logado.get("/").content.decode().count('aria-current="page"') == 1


def test_busca_do_celular_tem_rotulo(usuario_logado):
    html = usuario_logado.get("/").content.decode()
    assert '<label for="busca-movel"' in html
    assert 'id="busca-movel"' in html


def test_css_de_celular_16px_e_alvos_de_44px(usuario_logado):
    html = usuario_logado.get("/").content.decode()
    assert "@media (max-width:639px)" in html
    assert "font-size:16px!important" in html  # iPhone não dá zoom ao tocar no campo
    assert "min-height:44px" in html


def test_base_carrega_o_script_de_ux(usuario_logado, ux_js):
    html = usuario_logado.get("/").content.decode()
    assert re.search(r'<script src="[^"]*js/ux\.js"></script>\s*</body>', html)
    # o seletor de arquivo em português mudou para o ux.js (também vale após troca do HTMX)
    assert "Escolher arquivo" in ux_js and "Escolher arquivo" not in html


def test_ux_js_salvando_e_reabilita_no_voltar(ux_js):
    assert "Salvando…" in ux_js
    assert 'addEventListener("pageshow"' in ux_js  # Voltar do navegador destrava o botão
    assert "defaultPrevented" in ux_js  # respeita confirm() cancelado, HTMX e @submit.prevent
    assert '"post"' in ux_js  # só formulário que grava (filtros GET ficam como estão)


def test_ux_js_opcional_e_dicas(ux_js):
    assert "(opcional)" in ux_js
    assert "campo.required" in ux_js
    assert 'removeAttribute("title")' in ux_js
    assert '"Escape"' in ux_js  # dica fecha pelo teclado
    assert 'addEventListener("htmx:load"' in ux_js


def test_formulario_django_marca_obrigatorio_com_required(usuario_logado):
    """O "(opcional)" do ux.js depende de o Django pôr "required" só nos obrigatórios."""
    html = usuario_logado.get(reverse("multas:nova")).content.decode()
    campos = re.findall(r"<(?:input|select|textarea)\b[^>]*\bid=\"id_[^\"]+\"[^>]*>", html)
    assert campos
    assert any(" required" in c for c in campos)
    assert any(" required" not in c for c in campos)


ESCUROS = {"login.html", "bloqueio.html"}  # texto claro sobre fundo preto: 400 é o certo


def test_sem_cinza_claro_demais_nos_textos():
    achados = []
    for arquivo in TEMPLATES.rglob("*.html"):
        if arquivo.name in ESCUROS:
            continue
        for n, linha in enumerate(arquivo.read_text(encoding="utf-8").splitlines(), 1):
            if (
                re.search(r"(?<![\w:-])text-gray-400(?![\w-])", linha)
                and "main .text-gray-400" not in linha
            ):
                achados.append(f"{arquivo.relative_to(TEMPLATES)}:{n}")
    assert not achados, f"text-gray-400 some no sol — use 500/600: {achados}"


def test_aviso_tem_botao_de_fechar_grande():
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    botao = re.search(r'<button[^>]*class="([^"]*)"[^>]*aria-label="Fechar aviso"', base)
    assert "h-11" in botao.group(1) and "w-11" in botao.group(1)  # 44px para o dedo


# --- revisão: reforços ---


@pytest.mark.django_db
def test_todos_os_atalhos_da_barra_abrem(usuario_logado):
    """Link quebrado na barra seria pior que não ter barra: todos precisam abrir."""
    barra = _barra(usuario_logado.get("/").content.decode())
    links = set(re.findall(r'href="([^"]+)"', barra))
    assert len(links) >= 6
    for link in links:
        assert usuario_logado.get(link).status_code == 200, link


def test_menu_lancar_nao_finge_ser_menu_de_aplicativo(usuario_logado):
    # role="menu" promete setas do teclado que não existem; é uma lista de links comum
    barra = _barra(usuario_logado.get("/").content.decode())
    assert 'role="menu' not in barra and "aria-haspopup" not in barra
    assert ':aria-expanded="lancar"' in barra


def test_contraste_nao_atropela_hover_nem_tela_escura(usuario_logado):
    html = usuario_logado.get("/").content.decode()
    # :where segura a força da regra: hover:/sm: do Tailwind continuam valendo
    assert "main .text-gray-400:where(:not(" in html
    assert "main .text-gray-500:where(:not(" in html
    regra_500 = re.search(r"main \.text-gray-500:where\(:not\(([^)]*)\)\)", html).group(1)
    assert ".bg-gray-900 *" in regra_500  # cinza escurecido sumiria em fundo escuro


def test_salvando_nao_destrava_cedo_demais(ux_js):
    # 15s destravava no meio de um envio lento (3G do pátio) e permitia lançar duas vezes
    assert "15000" not in ux_js
    assert "multipart/form-data" in ux_js  # envio com foto nunca destrava sozinho


def test_rotulo_automatico_nao_vai_para_botao(ux_js):
    campos = re.search(r"const CAMPOS = \"([^\"]+)\"", ux_js).group(1)
    for tipo in ("submit", "button", "reset", "image"):
        assert f":not([type={tipo}])" in campos
    assert "matches(CAMPOS)" in ux_js and "querySelectorAll(CAMPOS)" in ux_js


def test_dica_nao_fica_com_dois_icones():
    """O "?" é posto pelo ux.js; um "ⓘ" escrito no texto com title viraria ícone duplo."""
    achados = []
    for arquivo in TEMPLATES.rglob("*.html"):
        for n, linha in enumerate(arquivo.read_text(encoding="utf-8").splitlines(), 1):
            if "title=" in linha and "ⓘ" in linha:
                achados.append(f"{arquivo.relative_to(TEMPLATES)}:{n}")
    assert not achados, achados
