from django.urls import path

from . import views

app_name = "pessoas"

urlpatterns = [
    path("", views.lista, name="lista"),
    path("novo/", views.novo, name="novo"),
    path("cnh/extrair/", views.cnh_extrair, name="cnh_extrair"),
    path("<int:cliente_id>/", views.detalhe, name="detalhe"),
    path("<int:cliente_id>/editar/", views.editar, name="editar"),
    path("<int:cliente_id>/condutores/novo/", views.condutor_novo, name="condutor_novo"),
    path("condutores/<int:condutor_id>/editar/", views.condutor_editar, name="condutor_editar"),
]
