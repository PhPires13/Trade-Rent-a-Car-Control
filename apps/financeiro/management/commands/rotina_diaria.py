"""Rotina diária (cron da plataforma): gera cobranças da semana e marca atrasos.

Uso: python manage.py rotina_diaria  (no Railway: "5 3 * * *" = 00:05 de Brasília)

Cada passo roda sozinho: se gerar as semanas der erro, os excedentes e os
atrasos ainda são atualizados. O erro vai para o log e o comando termina com
falha, para o cron acusar. Se a rotina parar de rodar, o painel avisa
(config.views.rotina_atrasada).
"""

import logging

from django.core.management.base import BaseCommand, CommandError

from apps.financeiro.services import gerar_cobrancas_semanais, marcar_atrasos
from apps.km.excedente import gerar_excedentes_pendentes

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Gera cobranças semanais e de excedente de km vencidas até hoje "
        "e atualiza atrasos/inadimplência."
    )

    def handle(self, *args, **options):
        passos = [
            ("cobranças semanais", gerar_cobrancas_semanais),
            ("excedentes de km", gerar_excedentes_pendentes),
            ("atrasos e inadimplência", marcar_atrasos),
        ]
        falhas = []
        for nome, passo in passos:
            try:
                resultado = passo()
            except Exception as erro:  # noqa: BLE001 — um passo não pode parar os outros
                logger.exception("Rotina diária: o passo '%s' falhou.", nome)
                self.stderr.write(self.style.ERROR(f"{nome}: FALHOU ({erro})"))
                falhas.append(nome)
                continue
            if resultado is None:
                self.stdout.write(f"{nome}: atualizado.")
            else:
                self.stdout.write(f"{nome}: {len(resultado)} criada(s).")
                for cobranca in resultado:
                    self.stdout.write(f"  - {cobranca}")
        if falhas:
            raise CommandError(f"Rotina diária com falha em: {', '.join(falhas)}.")
        self.stdout.write(self.style.SUCCESS("Rotina diária concluída."))
