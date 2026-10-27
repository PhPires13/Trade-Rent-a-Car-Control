"""Sessão expirada em telas com HTMX.

Sem isto, um clique HTMX (registrar KM, marcar algo na linha) depois que a
sessão caiu recebia o redirect para /entrar/, o HTMX seguia o redirect e
encaixava a página de login inteira dentro da linha ou da caixa da tela.
Aqui o redirect vira um HX-Redirect: o navegador vai para o login de
verdade e, depois de entrar, volta para a tela onde a pessoa estava.
"""

from urllib.parse import quote, urlsplit

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import resolve_url
from django.utils.http import url_has_allowed_host_and_scheme


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
        if request.headers.get("HX-Request") != "true" or resposta.status_code not in (301, 302):
            return resposta
        login = resolve_url(settings.LOGIN_URL)
        destino = urlsplit(resposta.get("Location", ""))
        if destino.path != login:
            return resposta
        nova = HttpResponse(status=204)
        volta = _volta(request)
        nova["HX-Redirect"] = f"{login}?next={quote(volta)}" if volta else login
        return nova
