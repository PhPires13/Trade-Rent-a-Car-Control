from django.urls import path

from . import views

app_name = "km"

urlpatterns = [
    path("", views.lista_mensal, name="lista"),
    path("registrar/<int:veiculo_id>/", views.registrar, name="registrar"),
    path("corrigir/<int:registro_id>/", views.corrigir, name="corrigir"),
    path("historico/<int:veiculo_id>/", views.historico, name="historico"),
]
