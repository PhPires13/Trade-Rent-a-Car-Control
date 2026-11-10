"""Etapa 18 (sinistros): limpar o "BO" digitado sem comer um "N" que faz parte
do número do boletim."""

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
        # "N" grudado no número faz parte dele
        ("BO N2026-1234", "N2026-1234"),
        ("BO-NO123", "NO123"),
        ("N2026-1234", "N2026-1234"),
        # abreviação de "número" continua saindo
        ("BO nº 55", "55"),
        ("BO nº45", "45"),
        ("BO n. 12", "12"),
        ("BO n.45", "45"),
        ("BO no 33", "33"),
        ("BO No. 7", "7"),
        ("B.O. Nº: 2026", "2026"),
    ],
)
def test_prefixo_bo_nao_come_n_do_numero(db, digitado, gravado):
    veiculo = Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol")
    form = SinistroForm(data=_dados(veiculo, digitado))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["boletim_ocorrencia"] == gravado


def test_numero_antigo_com_n_aparece_inteiro_na_lista(db):
    veiculo = Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol")
    sinistro = Sinistro.objects.create(
        veiculo=veiculo,
        data=HOJE - timedelta(days=3),
        tipo=Sinistro.Tipo.COLISAO,
        boletim_ocorrencia="BO N2026-1234",
    )
    assert sinistro.boletim_numero == "N2026-1234"


# --- revisão adversarial ---


def test_limpeza_do_bo_fica_no_modelo_sem_depender_da_tela():
    from apps.sinistros.models import numero_do_bo

    assert numero_do_bo("BO nº 45") == "45"
    assert numero_do_bo("BO N2026-1234") == "N2026-1234"
    assert numero_do_bo("Boletim 77") == "Boletim 77"
    assert numero_do_bo(None) == ""


def test_botao_aplicar_o_roubo_que_falha_nao_diz_que_salvou(usuario_logado):
    from decimal import Decimal

    from apps.alocacoes.models import Alocacao
    from apps.pessoas.models import Cliente

    veiculo = Veiculo.objects.create(placa="QXQ6C11", marca_modelo="Gol")
    cliente = Cliente.objects.create(nome="Bia", cpf_cnpj="111.222.333-55")
    Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=10),
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
    )
    # roubo de antes da locação atual: o serviço recusa encerrar
    sinistro = Sinistro.objects.create(
        veiculo=veiculo, data=HOJE - timedelta(days=40), tipo=Sinistro.Tipo.ROUBO
    )
    resposta = usuario_logado.post(
        f"/sinistros/{sinistro.pk}/aplicar-roubo/", {"volta": "/sinistros/"}, follow=True
    )
    textos = [str(m) for m in resposta.context["messages"]]
    assert any("não foi encerrada sozinha" in t for t in textos), textos
    assert not any("foi salvo" in t for t in textos), textos
