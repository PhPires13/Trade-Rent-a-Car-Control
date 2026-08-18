from django.urls import path

from . import views

app_name = "frota"

urlpatterns = [
    path("crlv/extrair/", views.crlv_extrair, name="crlv_extrair"),
    path("", views.hub, name="hub"),
    path("categorias/", views.categorias, name="categorias"),
    path("fornecedores/", views.fornecedores, name="fornecedores"),
    path("desmobilizacao/", views.ranking, name="ranking"),
    path("veiculo/novo/", views.veiculo_novo, name="veiculo_novo"),
    path("veiculo/<int:veiculo_id>/", views.detalhe, name="detalhe"),
    path("veiculo/<int:veiculo_id>/editar/", views.veiculo_editar, name="veiculo_editar"),
    path("veiculo/<int:veiculo_id>/ficha/", views.ficha, name="ficha"),
    path("veiculo/<int:veiculo_id>/vender/", views.vender, name="vender"),
]
