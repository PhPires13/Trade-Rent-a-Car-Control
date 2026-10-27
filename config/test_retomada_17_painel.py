"""Etapa 17 (retomada), frente painel-global: rotina parada, textos e links do painel, fuso."""

import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.financeiro import services
from apps.financeiro.models import Cobranca
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente
from config import views

RAIZ = Path(__file__).resolve().parents[1]
BH = ZoneInfo("America/Sao_Paulo")


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


def _alocacao(cliente, placa="QXQ7A17", dia=2):
    veiculo = Veiculo.objects.create(placa=placa, marca_modelo="Gol", km_atual=50_000)
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 9, 2),
        dia_vencimento=dia,
        valor_semanal=Decimal("700.00"),
        km_entrega=50_000,
    )


def _aluguel(alocacao, vencimento):
    return Cobranca.objects.create(
        cliente=alocacao.cliente,
        alocacao=alocacao,
        origem=Cobranca.Origem.ALUGUEL,
        descricao="Aluguel",
        valor=Decimal("700.00"),
        vencimento=vencimento,
        periodo_inicio=vencimento,
        periodo_fim=vencimento + timedelta(days=6),
    )


def _mudar_dia(alocacao, novo_dia, quando):
    """Muda o dia de vencimento como se o dono tivesse salvo no dia `quando`."""
    alocacao.dia_vencimento = novo_dia
    alocacao.save()
    criacao, mudanca = alocacao.history.order_by("history_date")
    criacao.history_date = datetime(2026, 9, 2, 10, tzinfo=BH)
    criacao.save()
    mudanca.history_date = datetime(quando.year, quando.month, quando.day, 10, tzinfo=BH)
    mudanca.save()


# ---------- rotina parada falsa depois de mudar o dia de vencimento ----------


def test_mudar_o_dia_de_vencimento_nao_acusa_rotina_parada(cliente):
    # pagava às quartas: a semana de 14/10 cobre 14 a 20/10. Na terça 20/10 o
    # dia passou para segunda; a rotina só lança na segunda 26/10 (dias 21 a
    # 01/11). Na sexta 23/10 nada está atrasado — antes o painel achava que a
    # segunda 19/10 tinha ficado sem lançar e acusava "rotina parada".
    alocacao = _alocacao(cliente, dia=2)
    _aluguel(alocacao, date(2026, 10, 14))
    _mudar_dia(alocacao, 0, date(2026, 10, 20))
    hoje = date(2026, 10, 23)

    assert services.gerar_cobrancas_semanais(hoje) == []  # a rotina também não lança nada
    previsto, nao_lancadas = views._alugueis_da_semana(
        (date(2026, 10, 19), date(2026, 10, 25)), hoje
    )
    assert nao_lancadas == 0
    assert previsto == Decimal("0")  # o próximo aluguel vence em 26/10, semana que vem


def test_painel_sem_aviso_de_rotina_parada_depois_de_mudar_o_dia(usuario_logado, cliente):
    # a mesma situação, vista na tela: o aviso vermelho não pode aparecer
    alocacao = _alocacao(cliente, dia=2)
    # a rotina rodou: a semana de 14/10 já foi marcada como atrasada
    Cobranca.objects.filter(pk=_aluguel(alocacao, date(2026, 10, 14)).pk).update(
        status=Cobranca.Status.ATRASADO
    )
    _mudar_dia(alocacao, 0, date(2026, 10, 20))
    with mock.patch("config.views.timezone.localdate", return_value=date(2026, 10, 23)):
        html = usuario_logado.get("/").content.decode()
    assert "rotina automática da noite parece parada" not in html


def test_painel_acusa_rotina_parada_quando_a_semana_nao_foi_lancada(usuario_logado, cliente):
    alocacao = _alocacao(cliente, dia=2)
    _aluguel(alocacao, date(2026, 10, 7))  # a de 14/10 não nasceu
    with mock.patch("config.views.timezone.localdate", return_value=date(2026, 10, 19)):
        html = usuario_logado.get("/").content.decode()
    assert "rotina automática da noite parece parada" in html


def test_mudanca_de_dia_sem_historico_nao_acusa_dias_ja_pagos(cliente):
    # dia mudado sem registro de quando (ex.: carga antiga): a conta antiga vale,
    # mas a segunda 19/10 que cairia dentro da semana já paga (até 20/10) só
    # cobraria a partir de 21/10 — não pode virar "rotina parada" no dia 21
    alocacao = _alocacao(cliente, dia=2)
    _aluguel(alocacao, date(2026, 10, 14))
    alocacao.history.all().delete()
    Alocacao.objects.filter(pk=alocacao.pk).update(dia_vencimento=0)
    alocacao.refresh_from_db()
    _, nao_lancadas = views._alugueis_da_semana(
        (date(2026, 10, 19), date(2026, 10, 25)), date(2026, 10, 21)
    )
    assert nao_lancadas == 0


def test_semana_esquecida_de_verdade_continua_acusando(cliente):
    alocacao = _alocacao(cliente, dia=2)
    _aluguel(alocacao, date(2026, 10, 7))  # 14/10 venceu e não foi lançada
    _, nao_lancadas = views._alugueis_da_semana(
        (date(2026, 10, 19), date(2026, 10, 25)), date(2026, 10, 19)
    )
    assert nao_lancadas == 1


def test_previsao_do_painel_nao_consulta_uma_vez_por_carro(cliente):
    semana = (date(2026, 10, 19), date(2026, 10, 25))
    _aluguel(_alocacao(cliente, "QXQ7A01"), date(2026, 10, 14))
    with CaptureQueriesContext(connection) as uma:
        views._alugueis_da_semana(semana, date(2026, 10, 19))
    for indice in range(2, 6):
        _aluguel(_alocacao(cliente, f"QXQ7A0{indice}"), date(2026, 10, 14))
    with CaptureQueriesContext(connection) as cinco:
        previsto, _ = views._alugueis_da_semana(semana, date(2026, 10, 19))
    assert len(cinco) == len(uma)
    assert previsto == Decimal("3500.00")  # 5 carros × semana cheia de 21/10


# ---------- textos e links do painel ----------


def test_card_em_atraso_leva_para_quem_deve(usuario_logado, cliente):
    # o total do card soma atraso e judicial: a lista de destino precisa ter os dois
    Cobranca.objects.create(
        cliente=cliente,
        origem=Cobranca.Origem.OUTRO,
        descricao="Lavagem",
        valor=Decimal("50.00"),
        vencimento=timezone.localdate() - timedelta(days=10),
        status=Cobranca.Status.JUDICIAL,
    )
    html = usuario_logado.get("/").content.decode()
    assert "?status=atrasado" not in html
    assert 'href="/financeiro/quem-deve/" class="rounded-lg bg-red-50' in html
    # e o destino mostra mesmo a dívida judicial que o card somou
    assert "Arlen" in usuario_logado.get("/financeiro/quem-deve/").content.decode()


def test_vigencias_do_painel_tem_link_para_resolver(usuario_logado, cliente):
    veiculo = Veiculo.objects.create(
        placa="QXQ7B17",
        marca_modelo="Gol",
        km_atual=50_000,
        licenciamento_vencimento=timezone.localdate() + timedelta(days=5),
    )
    cliente.status = Cliente.Status.ATIVO
    cliente.cnh_validade = timezone.localdate() + timedelta(days=3)
    cliente.save()
    html = usuario_logado.get("/").content.decode()
    assert f'href="/frota/veiculo/{veiculo.pk}/editar/"' in html
    assert f'href="/clientes/{cliente.pk}/editar/"' in html


def test_preventiva_do_painel_usa_o_texto_unico_do_km():
    painel = (RAIZ / "templates" / "painel.html").read_text(encoding="utf-8")
    assert "p.texto_km" in painel
    assert 'cut:"-"' not in painel  # a conta à parte é que podia dar "vencida há 0 km"


# ---------- fuso: nada de date.today() ----------


def test_codigo_nao_usa_date_today():
    """No Railway o relógio é UTC: depois das 21h date.today() já seria amanhã."""
    achados = []
    for arquivo in [*RAIZ.glob("apps/**/*.py"), *RAIZ.glob("config/**/*.py")]:
        # migrações também: a 0007 da frota roda no deploy (relógio em UTC)
        if arquivo.name == "formatos.py" or "__pycache__" in arquivo.parts:
            continue
        if arquivo == Path(__file__).resolve():
            continue
        if re.search(r"\bdate\.today\(\)", arquivo.read_text(encoding="utf-8")):
            achados.append(str(arquivo.relative_to(RAIZ)))
    assert achados == []


# ---------- contrato com dados da locadora ----------


def test_env_example_documenta_os_dados_da_locadora():
    exemplo = (RAIZ / ".env.example").read_text(encoding="utf-8")
    for variavel in ("EMPRESA_RAZAO_SOCIAL", "EMPRESA_CNPJ", "EMPRESA_ENDERECO"):
        assert variavel in exemplo
    assert "em branco" in exemplo
