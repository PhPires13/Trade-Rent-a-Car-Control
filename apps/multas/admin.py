from django import forms
from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import Multa, OrgaoAutuador


class OrgaoAutuadorForm(forms.ModelForm):
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
    form = OrgaoAutuadorForm
    list_display = ["nome", "esfera", "portal", "telefone"]
    search_fields = ["nome"]


@admin.register(Multa)
class MultaAdmin(SimpleHistoryAdmin):
    list_display = [
        "data",
        "veiculo",
        "cliente_alocacao",
        "descricao",
        "valor",
        "fici_status",
        "repasse",
    ]
    list_filter = ["resultado", "fici_status", "repasse", "orgao"]
    search_fields = ["veiculo__placa", "ait", "num_processamento", "descricao"]
