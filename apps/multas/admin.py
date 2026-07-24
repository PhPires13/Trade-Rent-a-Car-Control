from django import forms
from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import Multa, OrgaoAutuador


class OrgaoForm(forms.ModelForm):
    class Meta:
        model = OrgaoAutuador
        fields = [
            "nome",
            "esfera",
            "portal",
            "login",
            "senha",
            "email",
            "telefone",
            "procedimento",
            "endereco",
            "observacoes",
        ]
        widgets = {"senha": forms.PasswordInput(render_value=True)}


@admin.register(OrgaoAutuador)
class OrgaoAutuadorAdmin(SimpleHistoryAdmin):
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
