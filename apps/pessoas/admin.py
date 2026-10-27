from django import forms
from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from apps.financeiro import whatsapp

from .models import Cliente, CondutorAutorizado


class ClienteAdminForm(forms.ModelForm):
    """Mesma regra do cadastro: telefone com DDD, senão o WhatsApp não abre.

    Sem Meta: o admin monta os campos (modelform_factory); aqui só a validação.
    """

    def clean_telefone(self):
        telefone = self.cleaned_data["telefone"].strip()
        if telefone and not whatsapp.telefone_whatsapp(telefone):
            raise forms.ValidationError(whatsapp.MENSAGEM_TELEFONE_SEM_DDD)
        return telefone


@admin.register(Cliente)
class ClienteAdmin(SimpleHistoryAdmin):
    form = ClienteAdminForm
    list_display = ["nome", "cpf_cnpj", "telefone", "status", "cnh_validade", "dia_vencimento"]
    list_filter = ["status"]
    search_fields = ["nome", "cpf_cnpj", "telefone"]


@admin.register(CondutorAutorizado)
class CondutorAutorizadoAdmin(SimpleHistoryAdmin):
    list_display = ["nome", "cpf", "cliente", "contato"]
    search_fields = ["nome", "cpf"]
