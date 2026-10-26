"""Pendências da etapa 15 — frente km-manutenção: trava de envio duplicado e D27."""

import re
from datetime import date, timedelta

import pytest

from apps.frota.models import Veiculo
from apps.km.models import RegistroKm
from apps.manutencao.models import Manutencao

HOJE = date.today()


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="PND1K23", marca_modelo="Onix", km_atual=100_000)


def _dados(**extra):
    dados = {
        "tipo": "corretiva",
        "data": HOJE.isoformat(),
        "km": "100000",
        "descricao": "Troca da embreagem",
        "origem_custo": "particular",
        "responsavel": "empresa",
        "pagamento_custo": "pendente",
    }
    dados.update(extra)
    return dados


def _url(veiculo):
    return f"/manutencao/registrar/{veiculo.pk}/"


# --- clique duplo em "Salvar" -------------------------------------------------


def test_registrar_reenviado_nao_grava_em_dobro(usuario_logado, veiculo):
    html = usuario_logado.get(_url(veiculo)).content.decode()
    assert 'name="token"' in html
    dados = _dados(token="codigo-manut-1")
    assert usuario_logado.post(_url(veiculo), dados).status_code == 302
    segunda = usuario_logado.post(_url(veiculo), dados, follow=True)
    assert Manutencao.objects.count() == 1
    assert "envio repetido foi ignorado" in segunda.content.decode()
    # outro formulário (outro código) grava normalmente
    usuario_logado.post(_url(veiculo), _dados(token="codigo-manut-2"))
    assert Manutencao.objects.count() == 2


def test_formulario_com_erro_mantem_o_mesmo_codigo(usuario_logado, veiculo):
    resposta = usuario_logado.post(_url(veiculo), _dados(descricao="", token="codigo-x"))
    assert resposta.status_code == 200
    assert 'value="codigo-x"' in resposta.content.decode()
    assert not Manutencao.objects.exists()


def test_editar_nao_usa_codigo(usuario_logado, veiculo):
    manutencao = Manutencao.objects.create(
        veiculo=veiculo, tipo="corretiva", data=HOJE, km=100_000, descricao="Embreagem"
    )
    html = usuario_logado.get(f"/manutencao/editar/{manutencao.pk}/").content.decode()
    assert 'name="token"' not in html


# --- D27: KM fora do normal --------------------------------------------------


def test_km_com_digito_a_mais_pede_confirmacao(usuario_logado, veiculo):
    RegistroKm.objects.create(
        veiculo=veiculo,
        mes_referencia=HOJE.replace(day=1),
        data_leitura=HOJE - timedelta(days=3),
        km=100_000,
    )
    dados = _dados(km="1000000", token="d27-a")
    resposta = usuario_logado.post(_url(veiculo), dados)
    assert resposta.status_code == 200
    html = resposta.content.decode()
    assert "Confirmo o KM" in html
    assert "sobrou um dígito" in html
    assert not Manutencao.objects.exists()
    veiculo.refresh_from_db()
    assert veiculo.km_atual == 100_000
    # a pessoa confere e confirma: grava
    resposta = usuario_logado.post(_url(veiculo), {**dados, "confirmar_km": "on"})
    assert resposta.status_code == 302
    veiculo.refresh_from_db()
    assert veiculo.km_atual == 1_000_000


def test_km_normal_nao_pede_confirmacao(usuario_logado, veiculo):
    RegistroKm.objects.create(
        veiculo=veiculo,
        mes_referencia=HOJE.replace(day=1),
        data_leitura=HOJE - timedelta(days=10),
        km=100_000,
    )
    html = usuario_logado.get(_url(veiculo)).content.decode()
    assert "Confirmo o KM" not in html
    resposta = usuario_logado.post(_url(veiculo), _dados(km="102500", token="d27-b"))
    assert resposta.status_code == 302


def test_km_abaixo_do_atual_nao_pede_confirmacao(usuario_logado, veiculo):
    """Manutenção antiga com KM menor não mexe no carro — não há o que conferir."""
    resposta = usuario_logado.post(
        _url(veiculo), _dados(data=(HOJE - timedelta(days=90)).isoformat(), km="80000")
    )
    assert resposta.status_code == 302


def test_carro_sem_anotacao_de_km_compara_com_o_cadastro(usuario_logado, veiculo):
    resposta = usuario_logado.post(_url(veiculo), _dados(km="110000"))
    assert resposta.status_code == 302  # 10 mil a mais: normal
    resposta = usuario_logado.post(_url(veiculo), _dados(km="1100000", token="d27-c"))
    assert resposta.status_code == 200
    # sem leitura, a manutenção de 110.000 virou a última anotação: segue a régua por dia
    assert "Confirmo o KM" in resposta.content.decode()


def test_carro_nunca_anotado_compara_salto_com_o_cadastro(usuario_logado, veiculo):
    resposta = usuario_logado.post(_url(veiculo), _dados(km="1000000"))
    assert resposta.status_code == 200
    assert "cadastrado com 100.000 km" in resposta.content.decode()


def test_km_gigante_vira_mensagem_e_nao_erro(usuario_logado, veiculo):
    resposta = usuario_logado.post(_url(veiculo), _dados(km="99999999999", confirmar_km="on"))
    assert resposta.status_code == 200
    assert "confira os dígitos" in resposta.content.decode()
    assert not Manutencao.objects.exists()


def test_editar_sem_mexer_no_km_nao_pede_de_novo(usuario_logado, veiculo):
    manutencao = Manutencao.objects.create(
        veiculo=veiculo, tipo="corretiva", data=HOJE, km=100_000, descricao="Embreagem"
    )
    Veiculo.objects.filter(pk=veiculo.pk).update(km_atual=90_000)  # KM baixado depois
    resposta = usuario_logado.post(f"/manutencao/editar/{manutencao.pk}/", _dados())
    assert resposta.status_code == 302


def test_caixinha_so_aparece_para_km_fora_do_normal(usuario_logado, veiculo):
    """Erro de KM que "Confirmo" não resolve (dígitos demais) não mostra a caixinha."""
    resposta = usuario_logado.post(_url(veiculo), _dados(km="99999999999"))
    html = resposta.content.decode()
    assert "confira os dígitos" in html
    assert 'name="confirmar_km"' not in html


def test_confirmacao_segue_marcada_se_outro_campo_der_erro(usuario_logado, veiculo):
    resposta = usuario_logado.post(
        _url(veiculo), _dados(km="1000000", confirmar_km="on", descricao="", token="d27-d")
    )
    assert resposta.status_code == 200
    html = resposta.content.decode()
    caixinha = re.search(r'<input[^>]*name="confirmar_km"[^>]*>', html)
    assert caixinha and "checked" in caixinha.group(0)
    # corrige a descrição e salva sem precisar confirmar de novo
    resposta = usuario_logado.post(
        _url(veiculo), _dados(km="1000000", confirmar_km="on", token="d27-d")
    )
    assert resposta.status_code == 302
