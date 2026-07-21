"""Rotina diária (docs.md §10.1): gera cobranças da semana e atualiza atrasos.

Rodar 1x/dia (cron da plataforma). Sem Celery — a escala não justifica.
"""

from django.core.management.base import BaseCommand

from apps.financeiro.services import (
    atualizar_atrasos_e_inadimplencia,
    gerar_cobrancas_semanais,
)


class Command(BaseCommand):
    help = "Gera cobranças semanais do dia e atualiza atrasos/inadimplência."

    def handle(self, *args, **options):
        criadas = gerar_cobrancas_semanais()
        self.stdout.write(f"Cobranças de aluguel geradas: {len(criadas)}")
        clientes = atualizar_atrasos_e_inadimplencia()
        self.stdout.write(f"Clientes com cobrança atrasada: {len(clientes)}")
