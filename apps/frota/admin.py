from django import forms
from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from apps.financeiro.admin import coluna_em_reais
from apps.formatos import milhar

from .models import Categoria, Fornecedor, PagamentoIpva, Veiculo


@admin.register(Fornecedor)
class FornecedorAdmin(SimpleHistoryAdmin):
    list_display = ["nome", "tipo_servico", "contato"]
    search_fields = ["nome"]


@admin.register(Categoria)
class CategoriaAdmin(SimpleHistoryAdmin):
    list_display = ["nome", "valor_semanal_rs"]
    valor_semanal_rs = coluna_em_reais(
        "valor_semanal_referencia", "valor semanal de referência", "valor_semanal_referencia"
    )


class PagamentoIpvaInline(admin.TabularInline):
    model = PagamentoIpva
    extra = 0


@admin.register(PagamentoIpva)
class PagamentoIpvaAdmin(SimpleHistoryAdmin):
    list_display = ["veiculo", "ano", "parcela", "valor_rs", "vencimento", "pago_em"]
    valor_rs = coluna_em_reais("valor", "valor", "valor")
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

    def clean(self):
        """Vendido sem data da venda: a ficha e os relatórios não sabem até quando contar."""
        dados = super().clean()
        vendido = dados.get("status") == Veiculo.Status.VENDIDO
        if vendido and not dados.get("data_venda"):
            self.add_error("data_venda", "Carro vendido precisa da data da venda.")
        elif dados.get("status") and not vendido and dados.get("data_venda"):
            # com a data, o relatório conta a venda e para de cobrar a proteção
            self.add_error(
                "data_venda",
                "Só carro vendido tem data da venda — apague a data (e o valor da venda) "
                "ou deixe o status Vendido.",
            )
        return dados


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
        "km_atual_formatado",
        "mensalidade_rs",
    ]
    list_select_related = ["categoria"]
    mensalidade_rs = coluna_em_reais(
        "mensalidade_protecao", "mensalidade da proteção", "mensalidade_protecao"
    )

    @admin.display(description="KM atual", ordering="km_atual")
    def km_atual_formatado(self, obj):
        return milhar(obj.km_atual)

    list_filter = ["status", "uso", "categoria"]
    search_fields = ["placa", "marca_modelo", "renavam", "chassi"]
