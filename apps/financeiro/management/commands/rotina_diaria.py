"""Rotina diária (cron da plataforma): gera cobranças da semana e marca atrasos.

Uso: python manage.py rotina_diaria
"""

from django.core.management.base import BaseCommand

from apps.financeiro.services import gerar_cobrancas_semanais, marcar_atrasos


class Command(BaseCommand):
    help = "Gera cobranças semanais vencidas até hoje e atualiza atrasos/inadimplência."

    def handle(self, *args, **options):
        criadas = gerar_cobrancas_semanais()
        marcar_atrasos()
        self.stdout.write(
            self.style.SUCCESS(f"{len(criadas)} cobrança(s) gerada(s); atrasos atualizados.")
        )
