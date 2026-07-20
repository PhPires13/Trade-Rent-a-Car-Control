from django.urls import path

from . import views

app_name = "km"

urlpatterns = [
    path("", views.lista_mensal, name="lista"),
    path("registrar/<int:veiculo_id>/", views.registrar, name="registrar"),
    path("historico/<int:veiculo_id>/", views.historico, name="historico"),
]
