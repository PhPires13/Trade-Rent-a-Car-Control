from django.urls import path

from . import views

app_name = "sinistros"

urlpatterns = [
    path("", views.lista, name="lista"),
    path("novo/", views.novo, name="novo"),
    path("<int:sinistro_id>/auxilio/", views.solicitar_auxilio, name="solicitar_auxilio"),
    path("auxilio/<int:auxilio_id>/receber/", views.receber_auxilio, name="receber_auxilio"),
]
