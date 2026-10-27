"""Bugs da frente infra: rotina diária (C08)."""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from apps.financeiro.models import Cobranca
from apps.pessoas.models import Cliente

ROTINA = "apps.financeiro.management.commands.rotina_diaria"


@pytest.mark.django_db
def test_c08_erro_num_passo_nao_impede_os_outros_e_o_cron_acusa():
    cliente = Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")
    cobranca = Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.ALUGUEL,
        descricao="Aluguel",
        valor=Decimal("650.00"),
        vencimento=timezone.localdate() - timedelta(days=3),
    )
    with (
        mock.patch(f"{ROTINA}.gerar_cobrancas_semanais", side_effect=RuntimeError("banco caiu")),
        pytest.raises(CommandError, match="cobranças semanais"),
    ):
        call_command("rotina_diaria")
    cobranca.refresh_from_db()
    cliente.refresh_from_db()
    assert cobranca.status == Cobranca.Status.ATRASADO  # marcar_atrasos rodou mesmo assim
    assert cliente.status == Cliente.Status.INADIMPLENTE


@pytest.mark.django_db
def test_c08_rotina_sem_erro_diz_o_que_fez(capsys):
    call_command("rotina_diaria")
    saida = capsys.readouterr().out
    assert "cobranças semanais: 0 criada(s)" in saida
    assert "Rotina diária concluída" in saida
