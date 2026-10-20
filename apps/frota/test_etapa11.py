"""Etapa 11 — foto do carro, IPVA/licenciamento e mídia autenticada."""

import io
from datetime import date
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from apps.frota.models import PagamentoIpva, Veiculo
from apps.frota.views import VeiculoForm
from apps.relatorios import services


def foto(nome="carro.png"):
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), "#F0C948").save(buffer, format="PNG")
    return SimpleUploadedFile(nome, buffer.getvalue(), content_type="image/png")


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol")


def test_form_do_veiculo_aceita_foto(db):
    # o IPVA saiu do cadastro do carro: agora tem tela própria, ano a ano (decisão D06)
    form = VeiculoForm(
        {
            "placa": "RNB9J66",
            "marca_modelo": "Voyage",
            "uso": Veiculo.Uso.LOCACAO,
            "km_atual": 0,
            "chave_reserva": Veiculo.ChaveReserva.DUVIDA,
        },
        {"foto": foto()},
    )
    assert form.is_valid(), form.errors
    salvo = form.save()
    assert salvo.foto.name.startswith("veiculos/")


def test_foto_aparece_no_card_do_hub(usuario_logado, veiculo, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    veiculo.foto = foto()
    veiculo.save()
    html = usuario_logado.get("/frota/").content.decode()
    assert veiculo.foto.url in html


def test_midia_exige_login(client, veiculo, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    veiculo.foto = foto()
    veiculo.save()
    resposta = client.get(veiculo.foto.url)
    assert resposta.status_code == 302  # anônimo → tela de login
    assert resposta.url.startswith("/entrar/")


def test_midia_logado_serve_o_arquivo(usuario_logado, veiculo, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    veiculo.foto = foto()
    veiculo.save()
    resposta = usuario_logado.get(veiculo.foto.url)
    assert resposta.status_code == 200


def test_ipva_em_aberto_gera_alerta_de_vigencia(veiculo, db):
    from apps.frota.alertas import vigencias_a_vencer

    ipva = PagamentoIpva.objects.create(
        veiculo=veiculo, ano=2026, valor=Decimal("1200.00"), vencimento=date(2026, 7, 25)
    )
    alertas = vigencias_a_vencer(hoje=date(2026, 7, 20))
    assert any("IPVA 2026 do QXQ6C10" in a["descricao"] for a in alertas)
    # pago não alerta mais
    ipva.pago_em = date(2026, 7, 21)
    ipva.save()
    alertas = vigencias_a_vencer(hoje=date(2026, 7, 22))
    assert not any("IPVA" in a["descricao"] for a in alertas)


def test_licenciamento_vencendo_gera_alerta(veiculo, db):
    from apps.frota.alertas import vigencias_a_vencer

    veiculo.licenciamento_vencimento = date(2026, 8, 1)
    veiculo.save()
    alertas = vigencias_a_vencer(hoje=date(2026, 7, 20))
    assert any("Licenciamento do QXQ6C10" in a["descricao"] for a in alertas)


def test_ipva_pago_entra_na_despesa_do_mes(veiculo, db):
    PagamentoIpva.objects.create(
        veiculo=veiculo, ano=2026, valor=Decimal("1200.00"), pago_em=date(2026, 7, 10)
    )
    despesas = services.despesas_do_mes(2026, 7)
    assert despesas["total_ipva"] == Decimal("1200.00")
    assert despesas["total_geral"] == Decimal("1200.00")
    # fora do mês do pagamento não conta
    assert services.despesas_do_mes(2026, 6)["total_ipva"] == Decimal("0.00")
