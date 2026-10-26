from django.contrib import admin, messages
from simple_history.admin import SimpleHistoryAdmin

from apps.formatos import brl

from .models import Alocacao, TrocaTemporaria


class CicloDeVidaTravado(SimpleHistoryAdmin):
    """D15: alocação e troca mudam de situação só pelas telas normais.

    Apagar, encerrar, devolver ou reabrir pelo Admin deixava o carro "Alocado"
    sem motorista (ou "Disponível" com motorista) e não cancelava as semanas
    futuras. Aqui só dá para corrigir observações e valores; criar, apagar,
    mudar status/datas/KM e "reverter" pelo histórico ficam bloqueados.
    """

    campos_travados = []

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return [*super().get_readonly_fields(request, obj), *self.campos_travados]

    def revert_disabled(self, request, obj=None):
        return True


@admin.register(Alocacao)
class AlocacaoAdmin(CicloDeVidaTravado):
    list_display = [
        "veiculo",
        "cliente",
        "data_inicio",
        "data_termino",
        "valor_semanal",
        "status",
    ]
    list_filter = ["status", "limite_km"]
    search_fields = ["veiculo__placa", "cliente__nome"]
    campos_travados = [
        "veiculo",
        "cliente",
        "status",
        "data_inicio",
        "data_termino",
        "km_entrega",
        "km_devolucao",
    ]


@admin.register(TrocaTemporaria)
class TrocaTemporariaAdmin(CicloDeVidaTravado):
    list_display = [
        "alocacao",
        "veiculo_substituto",
        "data_retirada",
        "data_devolucao",
        "valor_semanal_ajustado",
    ]
    search_fields = ["veiculo_substituto__placa", "alocacao__cliente__nome"]
    campos_travados = [
        "alocacao",
        "veiculo_substituto",
        "data_retirada",
        "data_devolucao",
        "km_retirada",
        "km_devolucao",
    ]

    def save_model(self, request, obj, form, change):
        """Mudar o valor durante a troca refaz as semanas em aberto — mostra o novo valor."""
        super().save_model(request, obj, form, change)
        for cobranca in getattr(obj, "semanas_reajustadas", None) or []:
            messages.info(
                request,
                f"A semana com vencimento em {cobranca.vencimento:%d/%m/%Y} passou a "
                f"{brl(cobranca.valor)}.",
            )
