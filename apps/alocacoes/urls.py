from django.urls import path

from . import views

app_name = "alocacoes"

urlpatterns = [
    path("", views.lista, name="lista"),
    path("nova/", views.nova, name="nova"),
    path("<int:alocacao_id>/contrato/", views.contrato, name="contrato"),
    path("<int:alocacao_id>/vistoria/imprimir/", views.vistoria_imprimir, name="vistoria_imprimir"),
    path("<int:alocacao_id>/vistoria/nova/", views.vistoria_nova, name="vistoria_nova"),
    path("vistoria/extrair/", views.vistoria_extrair, name="vistoria_extrair"),
    path("<int:alocacao_id>/editar/", views.editar, name="editar"),
    path("<int:alocacao_id>/encerrar/", views.encerrar, name="encerrar"),
    path("<int:alocacao_id>/troca/", views.troca_nova, name="troca_nova"),
    path("troca/<int:troca_id>/devolver/", views.troca_devolver, name="troca_devolver"),
    path("veiculo/<int:veiculo_id>/linha-do-tempo/", views.timeline, name="timeline"),
]
