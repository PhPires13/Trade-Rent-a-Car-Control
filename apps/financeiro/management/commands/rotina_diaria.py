"""Rotina diária (cron da plataforma): gera cobranças da semana e marca atrasos.

Uso: python manage.py rotina_diaria
"""

from django.core.management.base import BaseCommand

from apps.financeiro.services import gerar_cobrancas_semanais, marcar_atrasos
from apps.km.excedente import gerar_excedentes_pendentes


class Command(BaseCommand):
    help = (
        "Gera cobranças semanais e de excedente de km vencidas até hoje "
        "e atualiza atrasos/inadimplência."
    )

    def handle(self, *args, **options):
        semanais = gerar_cobrancas_semanais()
        excedentes = gerar_excedentes_pendentes()
        marcar_atrasos()
        self.stdout.write(
            self.style.SUCCESS(
                f"{len(semanais)} cobrança(s) semanal(is), {len(excedentes)} excedente(s) "
                "de km; atrasos atualizados."
            )
        )
