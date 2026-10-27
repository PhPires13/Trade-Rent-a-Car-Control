from django import forms
from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import Categoria, Fornecedor, PagamentoIpva, Veiculo


@admin.register(Fornecedor)
class FornecedorAdmin(SimpleHistoryAdmin):
    list_display = ["nome", "tipo_servico", "contato"]
    search_fields = ["nome"]


@admin.register(Categoria)
class CategoriaAdmin(SimpleHistoryAdmin):
    list_display = ["nome", "valor_semanal_referencia"]


class PagamentoIpvaInline(admin.TabularInline):
    model = PagamentoIpva
    extra = 0


@admin.register(PagamentoIpva)
class PagamentoIpvaAdmin(SimpleHistoryAdmin):
    list_display = ["veiculo", "ano", "parcela", "valor", "vencimento", "pago_em"]
    list_filter = ["ano"]
    search_fields = ["veiculo__placa"]


class VeiculoAdminForm(forms.ModelForm):
    class Meta:
        model = Veiculo
        fields = "__all__"  # noqa: DJ007 — quem escolhe os campos é o VeiculoAdmin

    def clean_uso(self):
        """C14 também no Admin: carro alugado ou em troca aberta não vira "Fora de locação"."""
        from .views import impedimento_fora_de_locacao  # views importa outros apps

        uso = self.cleaned_data.get("uso")
        if self.instance.pk and uso == Veiculo.Uso.FORA_LOCACAO:
            motivo = impedimento_fora_de_locacao(self.instance)
            if motivo:
                raise forms.ValidationError(motivo)
        return uso


@admin.register(Veiculo)
class VeiculoAdmin(SimpleHistoryAdmin):
    form = VeiculoAdminForm
    inlines = [PagamentoIpvaInline]
    list_display = [
        "placa",
        "marca_modelo",
        "ano",
        "categoria",
        "uso",
        "status",
        "km_atual",
        "mensalidade_protecao",
    ]
    list_select_related = ["categoria"]
    list_filter = ["status", "uso", "categoria"]
    search_fields = ["placa", "marca_modelo", "renavam", "chassi"]
