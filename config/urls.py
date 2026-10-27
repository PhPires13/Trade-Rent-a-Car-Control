from django.contrib import admin
from django.urls import include, path

from config import views

# Retaguarda (/admin/) com a marca do sistema e em português
admin.site.site_header = "Trade Rent a Car — Retaguarda"
admin.site.site_title = "Retaguarda"
admin.site.index_title = "Correções e cadastros raros"


def _axes_em_portugues():
    """Da seção do axes (em inglês) fica só o que serve: destravar um login bloqueado.

    Só os rótulos mudam (nada de migração: o autodetector lê o Meta original).
    """
    from axes.models import AccessAttempt, AccessFailureLog, AccessLog
    from django.apps import apps
    from django.contrib.admin.exceptions import NotRegistered

    for modelo in (AccessLog, AccessFailureLog):
        try:
            admin.site.unregister(modelo)
        except NotRegistered:
            pass
    apps.get_app_config("axes").verbose_name = "Bloqueios de login"
    AccessAttempt._meta.verbose_name = "senha errada (bloqueio)"
    AccessAttempt._meta.verbose_name_plural = (
        "Senhas erradas — apague a linha para destravar o login antes dos 15 minutos"
    )


_axes_em_portugues()

urlpatterns = [
    path("", views.painel, name="painel"),
    path("midia/<path:caminho>", views.midia, name="midia"),
    path("buscar/", views.buscar, name="buscar"),
    path("entrar/", views.Entrar.as_view(), name="login"),
    path("sair/", views.sair, name="logout"),
    path("frota/", include("apps.frota.urls")),
    path("clientes/", include("apps.pessoas.urls")),
    path("alocacoes/", include("apps.alocacoes.urls")),
    path("financeiro/", include("apps.financeiro.urls")),
    path("km/", include("apps.km.urls")),
    path("manutencao/", include("apps.manutencao.urls")),
    path("multas/", include("apps.multas.urls")),
    path("sinistros/", include("apps.sinistros.urls")),
    path("relatorios/", include("apps.relatorios.urls")),
    path("admin/", admin.site.urls),
]
