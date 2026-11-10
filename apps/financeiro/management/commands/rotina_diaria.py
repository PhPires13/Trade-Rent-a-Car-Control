"""Rotina diária (cron da plataforma): gera cobranças da semana e marca atrasos.

Uso: python manage.py rotina_diaria  (no Railway: "5 3 * * *" = 00:05 de Brasília)

Cada passo roda sozinho: se gerar as semanas der erro, os excedentes e os
atrasos ainda são atualizados. O erro vai para o log e o comando termina com
falha, para o cron acusar. Se a rotina parar de rodar, o painel avisa
(config.views.rotina_atrasada).
"""

import logging

from django.core.management.base import BaseCommand, CommandError

from apps.financeiro.services import (
    FalhaEmAlguns,
    gerar_cobrancas_semanais,
    marcar_atrasos,
    recebimentos_incoerentes,
)
from apps.formatos import brl
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
            except FalhaEmAlguns as erro:
                # os outros itens do passo foram gravados; só os ruins ficaram de fora
                self.stderr.write(self.style.ERROR(f"{nome}: FALHOU em {', '.join(erro.falhas)}"))
                self.stdout.write(f"{nome}: {len(erro.criadas)} criada(s) nos demais.")
                falhas.append(nome)
                continue
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
        self._conferir_recebimentos()
        if falhas:
            raise CommandError(f"Rotina diária com falha em: {', '.join(falhas)}.")
        self.stdout.write(self.style.SUCCESS("Rotina diária concluída."))

    def _conferir_recebimentos(self):
        """Só avisa (não falha): recebimento cujo valor não fecha com o destino do dinheiro."""
        try:
            incoerentes = recebimentos_incoerentes()
        except Exception:  # noqa: BLE001 — a conferência nunca derruba a rotina
            logger.exception("Rotina diária: a conferência dos recebimentos falhou.")
            return
        for recebimento, diferenca in incoerentes:
            self.stderr.write(
                self.style.WARNING(
                    f"conferir o recebimento nº {recebimento.pk} ({recebimento}): o que ele "
                    "pagou nas cobranças + o que guardou (crédito/caução) não fecha com o "
                    f"valor — diferença de {brl(diferenca)}."
                )
            )
