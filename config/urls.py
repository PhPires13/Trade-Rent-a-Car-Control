from django import forms
from django.contrib import admin
from django.contrib.admin.options import BaseModelAdmin
from django.urls import include, path

from apps.campos import validar_tamanho_arquivo
from config import views

# Retaguarda (/admin/) com a marca do sistema e em português
admin.site.site_header = "Trade Rent a Car — Retaguarda"
admin.site.site_title = "Retaguarda"
admin.site.index_title = "Correções e cadastros raros"

_acoes_do_django = admin.ModelAdmin.get_action_choices


def _acoes_sem_tracos(self, request, default_choices=None):
    """Seletor "Ação" da Retaguarda sem o "---------" do Django (vale para toda lista)."""
    return _acoes_do_django(self, request, default_choices or [("", "— escolha a ação —")])


admin.ModelAdmin.get_action_choices = _acoes_sem_tracos

_campo_do_django = BaseModelAdmin.formfield_for_dbfield


def _campo_com_limite_de_arquivo(self, db_field, request, **kwargs):
    """Na Retaguarda também vale o limite de 10 MB por arquivo (CNH, foto, CRLV).

    Nas telas do sistema o FormularioBR já põe o limite; no Admin um PDF de 11 MB
    passava direto para o disco. Vale para todo campo de arquivo de toda tela do
    Admin, inclusive as linhas embutidas (inlines).
    """
    campo = _campo_do_django(self, db_field, request, **kwargs)
    if isinstance(campo, forms.FileField) and validar_tamanho_arquivo not in campo.validators:
        campo.validators.append(validar_tamanho_arquivo)
    return campo


BaseModelAdmin.formfield_for_dbfield = _campo_com_limite_de_arquivo


class BloqueioDeLoginAdmin(admin.ModelAdmin):
    """Lista das senhas erradas em português: só ver e apagar (apagar = destravar).

    As colunas do axes vinham em inglês ("Username", "Failed Logins"...). Os
    rótulos ficam aqui na tela, sem mexer nos campos do axes (nada de migração).
    """

    list_display = ("usuario", "ip", "erros", "quando")
    list_display_links = None
    search_fields = ("username", "ip_address")
    ordering = ("-attempt_time",)

    @admin.display(description="Usuário digitado", ordering="username")
    def usuario(self, tentativa):
        return tentativa.username

    @admin.display(description="De onde (IP)", ordering="ip_address")
    def ip(self, tentativa):
        return tentativa.ip_address

    @admin.display(description="Senhas erradas seguidas", ordering="failures_since_start")
    def erros(self, tentativa):
        return tentativa.failures_since_start

    @admin.display(description="Última tentativa", ordering="attempt_time")
    def quando(self, tentativa):
        return tentativa.attempt_time

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


def _axes_em_portugues():
    """Da seção do axes (em inglês) fica só o que serve: destravar um login bloqueado.

    Só os rótulos mudam (nada de migração: o autodetector lê o Meta original).
    """
    from axes.models import AccessAttempt, AccessFailureLog, AccessLog
    from django.apps import apps
    from django.contrib.admin.exceptions import NotRegistered

    for modelo in (AccessLog, AccessFailureLog, AccessAttempt):
        try:
            admin.site.unregister(modelo)
        except NotRegistered:
            pass
    admin.site.register(AccessAttempt, BloqueioDeLoginAdmin)
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
    path("saude/", views.saude, name="saude"),
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
