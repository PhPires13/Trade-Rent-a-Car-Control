from django.urls import path

from . import views

app_name = "alocacoes"

urlpatterns = [
    path("", views.lista, name="lista"),
    path("nova/", views.nova, name="nova"),
    path("<int:alocacao_id>/encerrar/", views.encerrar, name="encerrar"),
    path("<int:alocacao_id>/troca/", views.troca_nova, name="troca_nova"),
    path("troca/<int:troca_id>/devolver/", views.troca_devolver, name="troca_devolver"),
    path("veiculo/<int:veiculo_id>/linha-do-tempo/", views.timeline, name="timeline"),
]
