from django.urls import path

from . import views

app_name = "sinistros"

urlpatterns = [
    path("", views.lista, name="lista"),
    path("novo/", views.novo, name="novo"),
    path("quem-estava/", views.quem_estava, name="quem_estava"),
    path("<int:sinistro_id>/editar/", views.editar, name="editar"),
    path("<int:sinistro_id>/auxilio/", views.solicitar_auxilio, name="solicitar_auxilio"),
    path("<int:sinistro_id>/cobrar-franquia/", views.cobrar_franquia, name="cobrar_franquia"),
    path("<int:sinistro_id>/recuperado/", views.carro_recuperado, name="carro_recuperado"),
    path("auxilio/<int:auxilio_id>/receber/", views.receber_auxilio, name="receber_auxilio"),
]
