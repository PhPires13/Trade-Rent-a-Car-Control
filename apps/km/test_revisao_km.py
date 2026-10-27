"""Revisão da UX de KM: confirmação presa ao número, entrada torta, duplicidade e correção."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.financeiro.models import Cobranca
from apps.frota.models import Veiculo
from apps.km.correcao import suspeita_do_km
from apps.km.excedente import gerar_cobranca_excedente
from apps.km.forms import km_digitado
from apps.km.models import RegistroKm
from apps.pessoas.models import Cliente

HTMX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(
        placa="QXQ6C10", marca_modelo="Gol", km_compra=10_000, km_atual=10_000
    )


def registrar(veiculo, data_leitura, km):
    registro = RegistroKm(
        veiculo=veiculo,
        mes_referencia=data_leitura.replace(day=1),
        data_leitura=data_leitura,
        km=km,
    )
    registro.full_clean()
    registro.save()
    return registro


def postar(client, veiculo, km, data="2024-02-10", **extra):
    dados = {"data_leitura": data, "km": km, "mes": data[:7], **extra}
    return client.post(f"/km/registrar/{veiculo.pk}/", dados, **HTMX)


# --- A caixinha "Sim, o número está certo" vale só para o número avisado ----


def test_confirmar_e_trocar_o_numero_pede_conferencia_de_novo(usuario_logado, veiculo):
    registrar(veiculo, date(2024, 1, 10), 12_000)
    primeira = postar(usuario_logado, veiculo, "120000").content.decode()
    assert 'name="confirmo" value="120000"' in primeira
    # Corrigiu para outro número ainda errado, mas a caixinha ficou marcada.
    segunda = postar(usuario_logado, veiculo, "125000", confirmo="120000").content.decode()
    assert "Você digitou 125.000 km" in segunda
    assert not veiculo.registros_km.filter(mes_referencia=date(2024, 2, 1)).exists()
    postar(usuario_logado, veiculo, "125000", confirmo="125000")
    assert veiculo.registros_km.filter(km=125_000).exists()


def test_corrigir_confirmacao_tambem_presa_ao_numero(usuario_logado, veiculo):
    registrar(veiculo, date(2024, 1, 10), 12_000)
    fevereiro = registrar(veiculo, date(2024, 2, 10), 15_000)
    url = f"/km/corrigir/{fevereiro.pk}/"
    resposta = usuario_logado.post(url, {"data_leitura": "2024-02-10", "km": "150000"})
    assert 'name="confirmo" value="150000"' in resposta.content.decode()
    usuario_logado.post(url, {"data_leitura": "2024-02-10", "km": "160000", "confirmo": "150000"})
    fevereiro.refresh_from_db()
    assert fevereiro.km == 15_000
    usuario_logado.post(url, {"data_leitura": "2024-02-10", "km": "150000", "confirmo": "150000"})
    fevereiro.refresh_from_db()
    assert fevereiro.km == 150_000


# --- Número digitado torto ---------------------------------------------------


@pytest.mark.parametrize(
    ("texto", "km"),
    [("48900", 48_900), ("48.900", 48_900), ("48 900", 48_900), ("1.250.000", 1_250_000)],
)
def test_km_digitado_aceita_milhar(texto, km):
    assert km_digitado(texto) == km


@pytest.mark.parametrize("texto", ["12.5", "48,900", "-5", "1e5", "", "48.90", "99999999999999"])
def test_km_digitado_recusa_numero_torto(texto):
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        km_digitado(texto)


def test_ponto_fora_do_milhar_nao_vira_outro_numero(usuario_logado, veiculo):
    resposta = postar(usuario_logado, veiculo, "12.5")
    assert resposta.status_code == 200
    assert "só com números" in resposta.content.decode()
    assert not veiculo.registros_km.exists()


def test_km_gigante_nao_da_erro_500(usuario_logado, veiculo):
    resposta = postar(usuario_logado, veiculo, "99999999999999999999")
    assert resposta.status_code == 200
    assert "grande demais" in resposta.content.decode()
    assert not veiculo.registros_km.exists()


def test_campo_de_km_e_texto_com_teclado_numerico(usuario_logado, veiculo):
    conteudo = usuario_logado.get("/km/?mes=2024-02").content.decode()
    assert f'id="km-{veiculo.pk}-valor" type="text"' in conteudo
    assert 'inputmode="numeric"' in conteudo


def test_corrigir_aceita_ponto_de_milhar(usuario_logado, veiculo):
    """IntegerField do Django leria "12.000" como 12 — aqui tem de ser 12.000 km."""
    errado = registrar(veiculo, date(2024, 1, 10), 120_000)
    usuario_logado.post(
        f"/km/corrigir/{errado.pk}/", {"data_leitura": "2024-01-10", "km": "12.000"}
    )
    errado.refresh_from_db()
    assert errado.km == 12_000


def test_data_no_futuro_e_recusada(usuario_logado, veiculo):
    amanha = timezone.localdate() + timedelta(days=1)
    resposta = postar(usuario_logado, veiculo, "11000", data=amanha.isoformat())
    assert "ainda não chegou" in resposta.content.decode()
    assert not veiculo.registros_km.exists()


def test_erro_de_km_menor_e_legivel(usuario_logado, veiculo):
    registrar(veiculo, date(2024, 1, 10), 12_000)
    conteudo = postar(usuario_logado, veiculo, "11000").content.decode()
    assert "11.000 km é menor que a leitura anterior (12.000 km em 10/01/2024)" in conteudo


# --- Toque duplo / leitura repetida -------------------------------------------


def test_registrar_duas_vezes_avisa_sem_linha_em_branco(usuario_logado, veiculo):
    registrar(veiculo, date(2024, 1, 10), 12_000)
    postar(usuario_logado, veiculo, "14000")
    conteudo = postar(usuario_logado, veiculo, "14000").content.decode()
    assert "já tem leitura em 02/2024 (14.000 km em 10/02/2024)" in conteudo
    assert "12.000 km" in conteudo  # a "Última:" continua preenchida
    assert veiculo.registros_km.count() == 2


def test_leitura_em_outro_mes_avisa_que_o_mes_da_tela_continua_pendente(usuario_logado, veiculo):
    resposta = usuario_logado.post(
        f"/km/registrar/{veiculo.pk}/",
        {"data_leitura": "2024-01-31", "km": "11000", "mes": "2024-02"},
        **HTMX,
    )
    conteudo = resposta.content.decode()
    assert "ficou em 01/2024" in conteudo
    assert "02/2024 continua pendente" in conteudo


# --- Correção -----------------------------------------------------------------


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


def test_corrigir_so_a_observacao_nao_mexe_na_cobranca(usuario_logado, veiculo, cliente):
    Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2024, 1, 1),
        km_entrega=10_000,
        valor_semanal=Decimal("650.00"),
        limite_km=Alocacao.LimiteKm.LIMITADO,
        franquia_km_mensal=3_000,
        taxa_km_excedido=Decimal("0.50"),
    )
    registrar(veiculo, date(2024, 1, 10), 11_000)
    fevereiro = registrar(veiculo, date(2024, 2, 9), 25_000)
    cobranca = gerar_cobranca_excedente(fevereiro)
    usuario_logado.post(
        f"/km/corrigir/{fevereiro.pk}/",
        {
            "data_leitura": "2024-02-09",
            "km": "25000",
            "observacoes": "conferido",
            "confirmo": "25000",
        },
    )
    cobranca.refresh_from_db()
    fevereiro.refresh_from_db()
    assert fevereiro.observacoes == "conferido"
    assert cobranca.status != Cobranca.Status.CANCELADA
    assert fevereiro.cobranca_excedente_id == cobranca.pk
    assert Cobranca.objects.count() == 1


def test_corrigir_primeira_leitura_de_carro_sem_km_de_compra_nao_avisa(db):
    carro = Veiculo.objects.create(placa="RGD6H42", marca_modelo="Gol", km_atual=48_000)
    leitura = registrar(carro, date(2024, 1, 10), 48_000)
    leitura.km = 48_500
    assert suspeita_do_km(leitura) is None


def test_km_atual_preso_por_outro_lancamento_e_explicado(usuario_logado, veiculo, cliente):
    errado = registrar(veiculo, date(2024, 1, 10), 120_000)
    # a entrega foi digitada copiando o KM atual errado
    Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2024, 1, 15),
        km_entrega=120_000,
        valor_semanal=Decimal("650.00"),
    )
    resposta = usuario_logado.post(
        f"/km/corrigir/{errado.pk}/", {"data_leitura": "2024-01-10", "km": "12000"}, follow=True
    )
    mensagens = " ".join(str(m) for m in resposta.context["messages"])
    assert "continua 120.000 km porque outro lançamento" in mensagens


def test_corrigir_data_no_futuro_e_recusada(usuario_logado, veiculo):
    hoje = timezone.localdate()
    leitura = registrar(veiculo, hoje.replace(day=1), 11_000)
    if hoje.day == 31 or (hoje + timedelta(days=1)).month != hoje.month:
        pytest.skip("último dia do mês: não há amanhã dentro do mesmo mês")
    amanha = hoje + timedelta(days=1)
    resposta = usuario_logado.post(
        f"/km/corrigir/{leitura.pk}/", {"data_leitura": amanha.isoformat(), "km": "11000"}
    )
    assert "ainda não chegou" in resposta.content.decode()
