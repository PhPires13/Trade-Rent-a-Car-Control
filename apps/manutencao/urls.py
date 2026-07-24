from django.urls import path

from . import views

app_name = "manutencao"

urlpatterns = [
    path("preventivas/", views.preventivas, name="preventivas"),
    path("registrar/<int:veiculo_id>/", views.registrar, name="registrar"),
    path("repassar/<int:manutencao_id>/", views.repassar, name="repassar"),
    path("historico/<int:veiculo_id>/", views.historico, name="historico"),
]
