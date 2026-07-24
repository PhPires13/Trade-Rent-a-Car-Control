from django.urls import path

from . import views

app_name = "financeiro"

urlpatterns = [
    path("", views.cobrancas, name="cobrancas"),
    path("receber/", views.receber, name="receber"),
    path("cobranca/<int:cobranca_id>/encargo/", views.aplicar_encargo, name="aplicar_encargo"),
    path("cobranca/<int:cobranca_id>/judicial/", views.marcar_judicial, name="marcar_judicial"),
    path("nd/", views.nds, name="nds"),
    path("nd/nova/", views.nd_nova, name="nd_nova"),
    path("caucoes/", views.caucoes, name="caucoes"),
    path("caucoes/<int:caucao_id>/", views.caucao_detalhe, name="caucao_detalhe"),
    path("das/", views.das, name="das"),
    path("cliente/<int:cliente_id>/", views.extrato_cliente, name="extrato_cliente"),
]
