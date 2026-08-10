from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from config import views

urlpatterns = [
    path("", views.painel, name="painel"),
    path("buscar/", views.buscar, name="buscar"),
    path("entrar/", auth_views.LoginView.as_view(template_name="login.html"), name="login"),
    path("sair/", auth_views.LogoutView.as_view(), name="logout"),
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
