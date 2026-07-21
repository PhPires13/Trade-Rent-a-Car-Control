from django.urls import path

from . import views

app_name = "multas"

urlpatterns = [
    path("", views.lista, name="lista"),
    path("nova/", views.nova, name="nova"),
    path("emitir-nd/", views.emitir_nd, name="emitir_nd"),
]
