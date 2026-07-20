from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from config import views

urlpatterns = [
    path("", views.painel, name="painel"),
    path("entrar/", auth_views.LoginView.as_view(template_name="login.html"), name="login"),
    path("sair/", auth_views.LogoutView.as_view(), name="logout"),
    path("alocacoes/", include("apps.alocacoes.urls")),
    path("km/", include("apps.km.urls")),
    path("manutencao/", include("apps.manutencao.urls")),
    path("admin/", admin.site.urls),
]
