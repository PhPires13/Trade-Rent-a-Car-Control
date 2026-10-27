"""Sessão expirada em telas com HTMX (e nos fetch() de leitura de documento).

Sem isto, um clique HTMX (registrar KM, marcar algo na linha) depois que a
sessão caiu recebia o redirect para /entrar/, o HTMX seguia o redirect e
encaixava a página de login inteira dentro da linha ou da caixa da tela.
Aqui o redirect vira um HX-Redirect: o navegador vai para o login de
verdade e, depois de entrar, volta para a tela onde a pessoa estava.
"""

from urllib.parse import quote, urlsplit

from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, resolve_url
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import csrf

SESSAO_EXPIRADA = "Sua sessão expirou — entre de novo no sistema (usuário e senha) e repita."
PAGINA_VELHA = "A página ficou aberta muito tempo — recarregue a página (F5) e repita."


def _e_fetch(request):
    """fetch() das telas (ler CNH, CRLV, checklist) — não é clique HTMX nem navegação."""
    return request.headers.get("HX-Request") != "true" and request.headers.get(
        "Sec-Fetch-Mode"
    ) in ("cors", "same-origin")


def falha_csrf(request, reason=""):
    """CSRF_FAILURE_VIEW: o fetch() recebe JSON com o motivo (a tela mostra a
    mensagem em vez de "erro 403"); o resto vê a página 403_csrf.html."""
    if _e_fetch(request):
        return JsonResponse({"erro": PAGINA_VELHA}, status=403)
    if request.path == resolve_url(settings.LOGIN_URL):
        # tela de login velha (ou aberta em duas abas): "sessão expirou" confundia
        # quem só queria entrar. Logado vai ao Painel; senão, login novo com aviso.
        if request.user.is_authenticated:
            return redirect("painel")
        messages.info(
            request, "A página de entrada ficou aberta muito tempo. Tente entrar de novo."
        )
        return redirect(request.get_full_path())
    return csrf.csrf_failure(request, reason)


def _volta(request):
    """Caminho da tela aberta (só o path + query, nunca outro site)."""
    atual = request.headers.get("HX-Current-URL", "")
    partes = urlsplit(atual)
    if not atual or not url_has_allowed_host_and_scheme(
        atual, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return ""
    caminho = partes.path or "/"
    if not caminho.startswith("/") or caminho.startswith("//"):
        return ""
    return caminho + (f"?{partes.query}" if partes.query else "")


class HtmxLoginRedirectMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        resposta = self.get_response(request)
        if resposta.status_code not in (301, 302):
            return resposta
        login = resolve_url(settings.LOGIN_URL)
        destino = urlsplit(resposta.get("Location", ""))
        if destino.path != login:
            return resposta
        if request.headers.get("HX-Request") != "true":
            # fetch() das telas (ler CNH, CRLV, checklist): o navegador seguia o
            # redirect, recebia a página de login e a tela dizia "falha de conexão".
            # Com JSON 401 a tela mostra o motivo certo.
            if _e_fetch(request):
                return JsonResponse({"erro": SESSAO_EXPIRADA}, status=401)
            return resposta
        nova = HttpResponse(status=204)
        volta = _volta(request)
        nova["HX-Redirect"] = f"{login}?next={quote(volta)}" if volta else login
        return nova
