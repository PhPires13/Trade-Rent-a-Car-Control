"""UX do KM: aviso de número fora do normal, correção de leitura e registro sem recarregar."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.alocacoes.models import Alocacao
from apps.financeiro.models import AplicacaoRecebimento, Cobranca, Recebimento
from apps.frota.models import Veiculo
from apps.km.correcao import suspeita_do_km
from apps.km.excedente import gerar_cobranca_excedente
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


def novo(veiculo, data_leitura, km):
    return RegistroKm(
        veiculo=veiculo,
        mes_referencia=data_leitura.replace(day=1),
        data_leitura=data_leitura,
        km=km,
    )


# --- Quando o KM é "fora do normal" -----------------------------------------


def test_zero_a_mais_e_suspeito_com_texto_por_extenso(veiculo):
    registrar(veiculo, date(2026, 9, 13), 12_000)
    aviso = suspeita_do_km(novo(veiculo, date(2026, 10, 20), 120_000))
    assert "Você digitou 120.000 km" in aviso
    assert "estava com 12.000 km há 37 dias (em 13/09/2026)" in aviso
    assert "108.000 km rodados" in aviso
    assert aviso.endswith("Confirma?")


def test_uso_normal_nao_avisa(veiculo):
    registrar(veiculo, date(2026, 9, 13), 12_000)
    assert suspeita_do_km(novo(veiculo, date(2026, 10, 13), 18_000)) is None  # 200 km/dia


def test_salto_de_mais_de_3_vezes_a_media_do_carro_avisa(veiculo):
    registrar(veiculo, date(2026, 7, 1), 11_000)
    registrar(veiculo, date(2026, 8, 1), 14_100)  # ~100 km/dia
    registrar(veiculo, date(2026, 9, 1), 17_200)
    aviso = suspeita_do_km(novo(veiculo, date(2026, 10, 1), 30_000))  # ~427 km/dia
    assert "mais de 3 vezes o normal deste carro" in aviso
    assert "cerca de 100 km por dia" in aviso


def test_sem_leitura_anterior_compara_com_o_cadastro(veiculo):
    assert "cadastro do carro está com 10.000 km" in suspeita_do_km(
        novo(veiculo, date(2026, 10, 1), 100_000)
    )
    assert "menor" in suspeita_do_km(novo(veiculo, date(2026, 10, 1), 9_000))
    assert suspeita_do_km(novo(veiculo, date(2026, 10, 1), 13_000)) is None


def test_km_suspeito_nao_grava_sem_confirmar_e_volta_com_a_caixinha(usuario_logado, veiculo):
    registrar(veiculo, date(2024, 1, 10), 12_000)
    resposta = usuario_logado.post(
        f"/km/registrar/{veiculo.pk}/",
        {"data_leitura": "2024-02-10", "km": "120000", "mes": "2024-02"},
    )
    assert resposta.status_code == 200  # volta a tela, não redireciona
    conteudo = resposta.content.decode()
    assert "Você digitou 120.000 km" in conteudo
    assert 'name="confirmo"' in conteudo
    assert "Sim, o número está certo" in conteudo
    assert 'value="120000"' in conteudo  # o que foi digitado continua no campo
    assert veiculo.registros_km.count() == 1
    veiculo.refresh_from_db()
    assert veiculo.km_atual == 12_000


def test_km_suspeito_confirmado_grava(usuario_logado, veiculo):
    registrar(veiculo, date(2024, 1, 10), 12_000)
    resposta = usuario_logado.post(
        f"/km/registrar/{veiculo.pk}/",
        # a caixinha leva o número avisado (ver test_revisao_km.py)
        {"data_leitura": "2024-02-10", "km": "120000", "mes": "2024-02", "confirmo": "120000"},
    )
    assert resposta.status_code == 302
    assert veiculo.registros_km.filter(km=120_000).exists()


def test_km_com_ponto_de_milhar_e_aceito(usuario_logado, veiculo):
    usuario_logado.post(
        f"/km/registrar/{veiculo.pk}/",
        {"data_leitura": "2024-02-10", "km": "12.500", "mes": "2024-02"},
    )
    assert veiculo.registros_km.get().km == 12_500


def test_mensagem_de_sucesso_diz_quanto_rodou(usuario_logado, veiculo):
    registrar(veiculo, date(2024, 1, 10), 12_000)
    resposta = usuario_logado.post(
        f"/km/registrar/{veiculo.pk}/",
        {"data_leitura": "2024-02-10", "km": "14000", "mes": "2024-02"},
        follow=True,
    )
    mensagens = [str(m) for m in resposta.context["messages"]]
    assert any("rodou 2.000 km" in m for m in mensagens)


# --- Registro sem recarregar (htmx) ------------------------------------------


def test_htmx_devolve_so_a_linha_registrada_e_o_contador(usuario_logado, veiculo):
    Veiculo.objects.create(placa="RGD6H42", marca_modelo="Gol", km_compra=5_000, km_atual=5_000)
    resposta = usuario_logado.post(
        f"/km/registrar/{veiculo.pk}/",
        {"data_leitura": "2024-02-10", "km": "11000", "mes": "2024-02"},
        **HTMX,
    )
    assert resposta.status_code == 200
    conteudo = resposta.content.decode()
    assert "<html" not in conteudo
    assert "✓ KM de QXQ6C10 registrado: 11.000 km" in conteudo
    assert 'id="contador-pendentes" hx-swap-oob="true"' in conteudo
    assert "faltam 1" in conteudo
    assert veiculo.registros_km.count() == 1


def test_htmx_km_suspeito_volta_a_linha_com_aviso(usuario_logado, veiculo):
    resposta = usuario_logado.post(
        f"/km/registrar/{veiculo.pk}/",
        {"data_leitura": "2024-02-10", "km": "100000", "mes": "2024-02"},
        **HTMX,
    )
    conteudo = resposta.content.decode()
    assert resposta.status_code == 200
    assert "<html" not in conteudo
    assert f'id="pendente-{veiculo.pk}"' in conteudo
    assert "Sim, o número está certo" in conteudo
    assert "Registrar mesmo assim" in conteudo
    assert not veiculo.registros_km.exists()


def test_htmx_erro_fica_na_linha_sem_virar_mensagem_solta(usuario_logado, veiculo):
    registrar(veiculo, date(2024, 1, 10), 12_000)
    resposta = usuario_logado.post(
        f"/km/registrar/{veiculo.pk}/",
        {"data_leitura": "2024-02-10", "km": "11000", "mes": "2024-02"},
        **HTMX,
    )
    conteudo = resposta.content.decode()
    assert "menor que a leitura anterior" in conteudo
    assert 'aria-invalid="true"' in conteudo
    seguinte = usuario_logado.get("/km/?mes=2024-02")
    assert not list(seguinte.context["messages"])


def test_lista_tem_pendentes_com_htmx_e_cartoes_no_celular(usuario_logado, veiculo):
    conteudo = usuario_logado.get("/km/?mes=2024-02").content.decode()
    assert f'hx-post="/km/registrar/{veiculo.pk}/"' in conteudo
    assert "faltam 1" in conteudo
    assert "outra data?" in conteudo
    assert "sm:table-header-group" in conteudo  # leituras do mês viram cartão abaixo de sm


# --- Corrigir / apagar leitura -----------------------------------------------


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


def alocar_limitado(veiculo, cliente):
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2024, 1, 1),
        km_entrega=10_000,
        valor_semanal=Decimal("650.00"),
        limite_km=Alocacao.LimiteKm.LIMITADO,
        franquia_km_mensal=3_000,
        taxa_km_excedido=Decimal("0.50"),
    )


def test_corrigir_baixa_o_km_atual_e_recalcula_o_mes_seguinte(usuario_logado, veiculo):
    errado = registrar(veiculo, date(2024, 1, 10), 120_000)
    veiculo.refresh_from_db()
    assert veiculo.km_atual == 120_000

    resposta = usuario_logado.post(
        f"/km/corrigir/{errado.pk}/", {"data_leitura": "2024-01-10", "km": "12000"}
    )
    assert resposta.status_code == 302
    errado.refresh_from_db()
    veiculo.refresh_from_db()
    assert errado.km == 12_000
    assert veiculo.km_atual == 12_000


def test_corrigir_reapoia_a_leitura_seguinte(usuario_logado, veiculo):
    janeiro = registrar(veiculo, date(2024, 1, 10), 12_000)
    fevereiro = registrar(veiculo, date(2024, 2, 10), 15_000)
    usuario_logado.post(
        f"/km/corrigir/{janeiro.pk}/", {"data_leitura": "2024-01-10", "km": "12500"}
    )
    fevereiro.refresh_from_db()
    assert fevereiro.km_anterior == 12_500
    veiculo.refresh_from_db()
    assert veiculo.km_atual == 15_000  # a leitura de fevereiro continua mandando


def test_corrigir_nao_baixa_km_atual_vindo_de_outra_fonte(usuario_logado, veiculo):
    errado = registrar(veiculo, date(2024, 1, 10), 20_000)
    Veiculo.objects.filter(pk=veiculo.pk).update(km_atual=25_000)  # ex.: devolução depois
    usuario_logado.post(f"/km/corrigir/{errado.pk}/", {"data_leitura": "2024-01-10", "km": "12000"})
    veiculo.refresh_from_db()
    assert veiculo.km_atual == 25_000


def test_corrigir_para_numero_suspeito_pede_confirmacao(usuario_logado, veiculo):
    registrar(veiculo, date(2024, 1, 10), 12_000)
    fevereiro = registrar(veiculo, date(2024, 2, 10), 15_000)
    resposta = usuario_logado.post(
        f"/km/corrigir/{fevereiro.pk}/", {"data_leitura": "2024-02-10", "km": "150000"}
    )
    assert resposta.status_code == 200
    assert "Sim, o número está certo" in resposta.content.decode()
    fevereiro.refresh_from_db()
    assert fevereiro.km == 15_000


def test_corrigir_nao_deixa_trocar_de_mes(usuario_logado, veiculo):
    janeiro = registrar(veiculo, date(2024, 1, 10), 12_000)
    resposta = usuario_logado.post(
        f"/km/corrigir/{janeiro.pk}/", {"data_leitura": "2024-02-10", "km": "12000"}
    )
    assert resposta.status_code == 200
    assert "apague esta leitura e registre de novo" in resposta.content.decode()


def test_corrigir_refaz_excedente_em_aberto(usuario_logado, veiculo, cliente):
    alocar_limitado(veiculo, cliente)
    registrar(veiculo, date(2024, 1, 10), 11_000)
    errado = registrar(veiculo, date(2024, 2, 9), 25_000)  # 14.000 km em 30 dias
    antiga = gerar_cobranca_excedente(errado, hoje=date(2024, 2, 9))
    assert antiga.valor == Decimal("5500.00")

    resposta = usuario_logado.post(
        f"/km/corrigir/{errado.pk}/",
        {"data_leitura": "2024-02-09", "km": "15000"},
        follow=True,
    )
    antiga.refresh_from_db()
    errado.refresh_from_db()
    assert antiga.status == Cobranca.Status.CANCELADA
    assert errado.cobranca_excedente.valor == Decimal("500.00")  # 1.000 km além × R$ 0,50
    mensagens = " ".join(str(m) for m in resposta.context["messages"])
    assert "era R$ 5.500,00, agora é R$ 500,00" in mensagens


def test_corrigir_sem_excedente_cancela_a_cobranca(usuario_logado, veiculo, cliente):
    alocar_limitado(veiculo, cliente)
    registrar(veiculo, date(2024, 1, 10), 11_000)
    errado = registrar(veiculo, date(2024, 2, 9), 25_000)
    antiga = gerar_cobranca_excedente(errado)
    usuario_logado.post(f"/km/corrigir/{errado.pk}/", {"data_leitura": "2024-02-09", "km": "12000"})
    antiga.refresh_from_db()
    errado.refresh_from_db()
    assert antiga.status == Cobranca.Status.CANCELADA
    assert errado.cobranca_excedente is None


def _pagar(cobranca, cliente):
    recebimento = Recebimento.objects.create(
        cliente=cliente, data=date(2024, 2, 12), valor=cobranca.valor, forma="pix"
    )
    AplicacaoRecebimento.objects.create(
        recebimento=recebimento, cobranca=cobranca, valor=cobranca.valor
    )


def test_cobranca_paga_nao_e_mexida_e_bloqueia_apagar(usuario_logado, veiculo, cliente):
    alocar_limitado(veiculo, cliente)
    registrar(veiculo, date(2024, 1, 10), 11_000)
    errado = registrar(veiculo, date(2024, 2, 9), 25_000)
    cobranca = gerar_cobranca_excedente(errado)
    _pagar(cobranca, cliente)

    resposta = usuario_logado.post(f"/km/corrigir/{errado.pk}/", {"apagar": "1"}, follow=True)
    assert RegistroKm.objects.filter(pk=errado.pk).exists()
    assert "não dá para apagar" in " ".join(str(m) for m in resposta.context["messages"])

    resposta = usuario_logado.post(
        f"/km/corrigir/{errado.pk}/", {"data_leitura": "2024-02-09", "km": "15000"}, follow=True
    )
    cobranca.refresh_from_db()
    assert cobranca.status != Cobranca.Status.CANCELADA
    assert "não foi mexida" in " ".join(str(m) for m in resposta.context["messages"])


def test_apagar_leitura_reapoia_o_mes_seguinte_e_volta_a_pendencia(usuario_logado, veiculo):
    registrar(veiculo, date(2024, 1, 10), 12_000)
    errado = registrar(veiculo, date(2024, 2, 10), 120_000)
    resposta = usuario_logado.post(f"/km/corrigir/{errado.pk}/", {"apagar": "1", "voltar": "lista"})
    assert resposta.url == "/km/?mes=2024-02"
    assert not RegistroKm.objects.filter(pk=errado.pk).exists()
    veiculo.refresh_from_db()
    assert veiculo.km_atual == 12_000


def test_apagar_leitura_do_meio_reencadeia(usuario_logado, veiculo):
    registrar(veiculo, date(2024, 1, 10), 12_000)
    meio = registrar(veiculo, date(2024, 2, 10), 14_000)
    marco = registrar(veiculo, date(2024, 3, 10), 16_000)
    usuario_logado.post(f"/km/corrigir/{meio.pk}/", {"apagar": "1"})
    marco.refresh_from_db()
    assert marco.km_anterior == 12_000
    assert marco.dias == (date(2024, 3, 10) - date(2024, 1, 10)).days


def test_tela_de_corrigir_e_botoes_nas_listas(usuario_logado, veiculo):
    registro = registrar(veiculo, date.today() - timedelta(days=1), 12_000)
    assert usuario_logado.get(f"/km/corrigir/{registro.pk}/").status_code == 200
    historico = usuario_logado.get(f"/km/historico/{veiculo.pk}/").content.decode()
    assert f"/km/corrigir/{registro.pk}/" in historico
    lista = usuario_logado.get(f"/km/?mes={registro.mes_referencia:%Y-%m}").content.decode()
    assert f"/km/corrigir/{registro.pk}/?voltar=lista" in lista
