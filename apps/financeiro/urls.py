from django.urls import path

from . import views

app_name = "financeiro"

urlpatterns = [
    path("cobrancas/", views.painel_cobrancas, name="cobrancas"),
    path("baixa/", views.baixa_recebimento, name="baixa"),
    path("cobrancas/<int:cobranca_id>/encargo/", views.encargo, name="encargo"),
    path("cobrancas/<int:cobranca_id>/judicial/", views.marcar_judicial, name="judicial"),
    path("notas/", views.lista_notas, name="notas"),
    path("caucoes/", views.lista_caucoes, name="caucoes"),
    path("das/", views.relatorio_das, name="das"),
    path("das/exportar/", views.exportar_das_csv, name="das_csv"),
]
