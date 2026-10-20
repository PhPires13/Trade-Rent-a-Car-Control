"""IP real de quem acessa, para o bloqueio de login (django-axes, decisão D29).

Atrás do proxy do Railway o REMOTE_ADDR é sempre o do proxy — todo mundo teria o
mesmo IP e o bloqueio por usuário+IP voltaria a travar os donos. O proxy
ACRESCENTA o IP de quem conectou no fim do X-Forwarded-For; o começo do cabeçalho
vem do navegador e pode ser inventado. Por isso só a entrada de número
PROXIES_CONFIAVEIS, contando do fim, é confiável.
"""

from django.conf import settings


def ip_do_cliente(request):
    proxies = getattr(settings, "PROXIES_CONFIAVEIS", 0)
    encaminhado = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if proxies > 0 and encaminhado:
        ips = [ip.strip() for ip in encaminhado.split(",") if ip.strip()]
        if len(ips) >= proxies:
            return ips[-proxies]
    return request.META.get("REMOTE_ADDR")
