from django.urls import path

from . import views

app_name = "frota"

urlpatterns = [
    path("desmobilizacao/", views.ranking, name="ranking"),
    path("veiculo/<int:veiculo_id>/ficha/", views.ficha, name="ficha"),
    path("veiculo/<int:veiculo_id>/vender/", views.vender, name="vender"),
]
