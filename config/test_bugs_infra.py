"""Bugs da frente infra: painel, mídia, login e documentação (C08, C22, C28, D29, D32)."""

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pytest
from django.test import Client, RequestFactory, override_settings
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.financeiro.models import Cobranca
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente
from config import views
from config.seguranca import ip_do_cliente


class SegundaFixa(date):
    """Segunda-feira 19/10/2026 — semana de 19 a 25/10."""

    @classmethod
    def today(cls):
        return date(2026, 10, 19)


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


def _alocacao(cliente, data_inicio, dia_vencimento):
    veiculo = Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_atual=50_000)
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=data_inicio,
        dia_vencimento=dia_vencimento,
        valor_semanal=Decimal("650.00"),
        km_entrega=50_000,
    )


def _aluguel(cliente, vencimento, alocacao=None, **extra):
    return Cobranca.objects.create(
        cliente=cliente,
        alocacao=alocacao,
        origem=Cobranca.Origem.ALUGUEL,
        descricao="Aluguel",
        valor=Decimal("650.00"),
        vencimento=vencimento,
        **extra,
    )


# ---------- C08: rotina parada ----------


def test_c08_painel_avisa_e_conta_atraso_quando_a_rotina_nao_rodou(usuario_logado, cliente):
    # venceu há 5 dias e continua "pendente": a rotina não marcou o atraso
    _aluguel(cliente, timezone.localdate() - timedelta(days=5))
    resposta = usuario_logado.get("/")
    assert resposta.context["rotina_atrasada"] is True
    assert resposta.context["atrasado_total"] == Decimal("650.00")
    assert "rotina automática da noite parece parada" in resposta.content.decode()


def test_c08_semana_de_aluguel_nao_lancada_tambem_avisa(usuario_logado, cliente):
    _alocacao(
        cliente, timezone.localdate() - timedelta(days=20), (timezone.localdate().weekday() + 3) % 7
    )
    assert usuario_logado.get("/").context["rotina_atrasada"] is True


def test_c08_sem_aviso_quando_a_rotina_esta_em_dia(usuario_logado, cliente):
    _aluguel(cliente, timezone.localdate() - timedelta(days=5), status=Cobranca.Status.ATRASADO)
    _aluguel(cliente, timezone.localdate())  # vence hoje: ainda não é atraso
    resposta = usuario_logado.get("/")
    assert resposta.context["rotina_atrasada"] is False
    assert "parece parada" not in resposta.content.decode()


# ---------- D32: previstos da semana ----------


def test_d32_previstos_somam_alugueis_que_ainda_vao_vencer(cliente):
    # vence às sextas; a última semana lançada foi 16/10, a próxima é 23/10
    alocacao = _alocacao(cliente, date(2026, 9, 4), dia_vencimento=4)
    _aluguel(cliente, date(2026, 10, 16), alocacao=alocacao, status=Cobranca.Status.PAGO)
    semana = (date(2026, 10, 19), date(2026, 10, 25))
    previsto, nao_lancadas = views._alugueis_da_semana(semana, date(2026, 10, 19))
    assert previsto == Decimal("650.00")
    assert nao_lancadas == 0


def test_d32_alocacao_que_termina_antes_do_vencimento_nao_entra(cliente):
    alocacao = _alocacao(cliente, date(2026, 9, 4), dia_vencimento=4)
    alocacao.data_termino = date(2026, 10, 22)
    alocacao.save()
    _aluguel(cliente, date(2026, 10, 16), alocacao=alocacao)
    previsto, _ = views._alugueis_da_semana(
        (date(2026, 10, 19), date(2026, 10, 25)), date(2026, 10, 19)
    )
    assert previsto == Decimal("0")


def test_d32_card_mostra_o_previsto_da_semana_inteira(usuario_logado, cliente):
    alocacao = _alocacao(cliente, date(2026, 9, 4), dia_vencimento=4)
    _aluguel(cliente, date(2026, 10, 16), alocacao=alocacao, status=Cobranca.Status.PAGO)
    with mock.patch("config.views.timezone.localdate", return_value=SegundaFixa.today()):
        resposta = usuario_logado.get("/")
    assert resposta.context["semana_total"] == Decimal("650.00")
    assert "inclui R$ 650,00 de aluguéis que ainda vão vencer" in resposta.content.decode()


# ---------- C22: documentos fora do cache do navegador ----------


def test_c22_midia_nao_fica_no_cache_do_navegador(usuario_logado, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    (tmp_path / "cnh.jpg").write_bytes(b"\xff\xd8\xff foto")
    resposta = usuario_logado.get("/midia/cnh.jpg")
    assert resposta.status_code == 200
    assert "no-store" in resposta["Cache-Control"]
    assert "private" in resposta["Cache-Control"]


# ---------- D29: bloqueio de login por usuário + IP ----------


@pytest.mark.django_db
@override_settings(AXES_ENABLED=True)
def test_d29_estranho_errando_a_senha_nao_trava_o_dono(django_user_model, settings):
    from axes.utils import reset

    django_user_model.objects.create_user(username="luciana", password="senha-forte-123")
    atacante = Client(REMOTE_ADDR="203.0.113.9")
    dona = Client(REMOTE_ADDR="198.51.100.7")
    try:
        for _ in range(settings.AXES_FAILURE_LIMIT):
            atacante.post("/entrar/", {"username": "luciana", "password": "chute"})
        travado = atacante.post("/entrar/", {"username": "luciana", "password": "senha-forte-123"})
        assert travado.status_code == 429  # o atacante fica bloqueado
        entrou = dona.post("/entrar/", {"username": "luciana", "password": "senha-forte-123"})
        assert entrou.status_code == 302  # a dona, de outro endereço, entra
    finally:
        reset()


def test_d29_ip_vem_da_entrada_do_proxy_e_nao_da_forjada(settings):
    pedido = RequestFactory().get(
        "/", HTTP_X_FORWARDED_FOR="1.2.3.4, 203.0.113.9", REMOTE_ADDR="10.0.0.1"
    )
    settings.PROXIES_CONFIAVEIS = 1
    assert ip_do_cliente(pedido) == "203.0.113.9"  # "1.2.3.4" foi o navegador que mandou
    settings.PROXIES_CONFIAVEIS = 0
    assert ip_do_cliente(pedido) == "10.0.0.1"  # rodando local, sem proxy


# ---------- C28: cron em UTC documentado ----------


def test_c28_documentacao_diz_que_o_cron_e_em_utc():
    etapas = (Path(__file__).resolve().parents[1] / "ETAPAS.md").read_text()
    assert "5 3 * * *" in etapas
    assert "UTC" in etapas


# ---------- revisão: contrato antigo com "cobrar a partir de" ----------


def test_revisao_contrato_antigo_com_cobrar_a_partir_de_nao_acusa_rotina_parada(cliente):
    # contrato que já rodava antes do sistema: começou em março, mas o aluguel
    # só é lançado a partir da quinta 22/10 (decisão D01). Nada foi lançado
    # ainda e nada está atrasado — a rotina está em dia.
    alocacao = _alocacao(cliente, date(2026, 3, 2), dia_vencimento=0)  # vence às segundas
    alocacao.cobrar_a_partir_de = date(2026, 10, 22)
    alocacao.save()
    previsto, nao_lancadas = views._alugueis_da_semana(
        (date(2026, 10, 19), date(2026, 10, 25)), date(2026, 10, 19)
    )
    assert nao_lancadas == 0
    # 1ª cobrança vence na própria quinta 22/10 e é proporcional: 22 a 25/10 = 4 dias
    assert previsto == (Decimal("650.00") * 4 / 7).quantize(Decimal("0.01"))


def test_revisao_semana_apos_o_termino_nao_conta_como_nao_lancada(cliente):
    # alocação ainda ativa com término marcado: a semana de 16/10 começaria no
    # próprio dia do término, então não é devida (cobrança pré-paga)
    alocacao = _alocacao(cliente, date(2026, 9, 4), dia_vencimento=4)
    alocacao.data_termino = date(2026, 10, 16)
    alocacao.save()
    _aluguel(cliente, date(2026, 10, 9), alocacao=alocacao)
    previsto, nao_lancadas = views._alugueis_da_semana(
        (date(2026, 10, 19), date(2026, 10, 25)), date(2026, 10, 19)
    )
    assert (previsto, nao_lancadas) == (Decimal("0"), 0)
