"""Etapa 17 (navegador) — sinistros: "BO" digitado não duplica na lista,
seletor de carro com a placa primeiro e "Quem está envolvido" sem tracinhos."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.frota.models import Veiculo
from apps.sinistros.models import Sinistro
from apps.sinistros.views import SinistroForm

HOJE = timezone.localdate()


def _dados(veiculo, bo):
    return {
        "veiculo": veiculo.pk,
        "data": (HOJE - timedelta(days=3)).isoformat(),
        "cliente": "",
        "tipo": Sinistro.Tipo.COLISAO,
        "envolvido": Sinistro.Envolvido.ASSOCIADO,
        "responsabilidade": Sinistro.Responsabilidade.INDEFINIDA,
        "descricao": "batida",
        "boletim_ocorrencia": bo,
        "responsavel_custo": "empresa",
    }


@pytest.mark.parametrize(
    "digitado, gravado",
    [
        ("BO 2026-123", "2026-123"),
        ("B.O. 123/2026", "123/2026"),
        ("bo nº 55", "55"),
        ("BO: 9876", "9876"),
        ("BO2026", "2026"),
        ("123/2026", "123/2026"),
        ("Boletim 7", "Boletim 7"),
        ("BOA123", "BOA123"),
        ("BO", ""),
        ("  b.o 77 ", "77"),
        ("", ""),
    ],
)
def test_prefixo_bo_digitado_e_retirado(db, digitado, gravado):
    veiculo = Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol")
    form = SinistroForm(data=_dados(veiculo, digitado))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["boletim_ocorrencia"] == gravado


def test_lista_nao_mostra_bo_bo(usuario_logado):
    veiculo = Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol")
    resposta = usuario_logado.post("/sinistros/novo/", _dados(veiculo, "BO 2026-1"))
    assert resposta.status_code == 302
    html = usuario_logado.get("/sinistros/").content.decode()
    assert "BO 2026-1" in html
    assert "BO BO" not in html


def test_dica_do_bo_pede_so_o_numero(db):
    assert "Só o número" in SinistroForm().fields["boletim_ocorrencia"].help_text


def test_seletor_de_carro_mostra_a_placa_primeiro(db):
    Veiculo.objects.create(placa="ABC1D23", marca_modelo="Onix")
    form = SinistroForm()
    rotulos = [rotulo for _, rotulo in form.fields["veiculo"].choices]
    assert "ABC1D23 — Onix" in rotulos
    envolvido = [rotulo for _, rotulo in form.fields["envolvido"].choices]
    assert "---------" not in envolvido
