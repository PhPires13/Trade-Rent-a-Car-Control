from django.urls import path

from . import views

app_name = "multas"

urlpatterns = [
    path("", views.lista, name="lista"),
    path("nova/", views.nova, name="nova"),
    path("<int:multa_id>/fici/", views.indicar_fici, name="indicar_fici"),
    path("<int:multa_id>/nic/", views.registrar_nic, name="registrar_nic"),
    path("<int:multa_id>/paga/", views.marcar_paga, name="marcar_paga"),
    path("gerar-nd/", views.gerar_nd, name="gerar_nd"),
    path("orgaos/", views.orgaos, name="orgaos"),
]
