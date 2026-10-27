from django.urls import path

from . import views

app_name = "manutencao"

urlpatterns = [
    path("preventivas/", views.preventivas, name="preventivas"),
    path("plano/", views.plano, name="plano"),
    path("registrar/", views.escolher_carro, name="escolher_carro"),
    path("registrar/<int:veiculo_id>/", views.registrar, name="registrar"),
    path("apagar/<int:manutencao_id>/", views.apagar, name="apagar"),
    path("editar/<int:manutencao_id>/", views.editar, name="editar"),
    path("voltou/<int:manutencao_id>/", views.voltou, name="voltou"),
    path("repassar/<int:manutencao_id>/", views.repassar, name="repassar"),
    path("historico/<int:veiculo_id>/", views.historico, name="historico"),
]
