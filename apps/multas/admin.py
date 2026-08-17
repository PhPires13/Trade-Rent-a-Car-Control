from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .forms import OrgaoForm
from .models import Multa, OrgaoAutuador


@admin.register(OrgaoAutuador)
class OrgaoAutuadorAdmin(SimpleHistoryAdmin):
    # Mesmo form das telas: a senha não volta no HTML e campo em branco mantém a
    # atual. O form antigo daqui usava PasswordInput(render_value=True) e devolvia
    # a senha do portal em claro no fonte da página.
    form = OrgaoForm
    list_display = ["nome", "esfera", "portal", "telefone"]


@admin.register(Multa)
class MultaAdmin(SimpleHistoryAdmin):
    list_display = [
        "veiculo",
        "cliente",
        "data_infracao",
        "descricao",
        "valor",
        "resultado",
        "fici_status",
        "pagamento",
    ]
    list_filter = ["resultado", "fici_status", "pagamento", "orgao"]
    search_fields = ["veiculo__placa", "cliente__nome", "ait", "codigo"]
