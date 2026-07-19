from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from config import views

urlpatterns = [
    path("", views.painel, name="painel"),
    path("entrar/", auth_views.LoginView.as_view(template_name="login.html"), name="login"),
    path("sair/", auth_views.LogoutView.as_view(), name="logout"),
    path("admin/", admin.site.urls),
]
