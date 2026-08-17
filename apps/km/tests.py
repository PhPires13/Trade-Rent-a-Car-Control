from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from apps.frota.models import Veiculo
from apps.km.models import RegistroKm, veiculos_com_leitura_pendente
from apps.km.views import _data_sugerida


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_compra=50_000)


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


def test_primeiro_registro_usa_km_da_compra(veiculo):
    registro = registrar(veiculo, date(2026, 7, 15), 52_000)
    assert registro.km_anterior == 50_000
    assert registro.km_utilizado == 2_000


def test_km_ant_e_dias_vem_do_mes_anterior(veiculo):
    registrar(veiculo, date(2026, 6, 10), 52_000)
    registro = registrar(veiculo, date(2026, 7, 12), 55_200)
    assert registro.km_anterior == 52_000
    assert registro.dias == 32
    assert registro.km_utilizado == 3_200
    assert round(registro.media_dia) == 100
    assert round(registro.media_mes) == 3_000


def test_um_registro_por_veiculo_por_mes(veiculo):
    registrar(veiculo, date(2026, 7, 10), 52_000)
    with pytest.raises(IntegrityError):
        RegistroKm.objects.create(
            veiculo=veiculo,
            mes_referencia=date(2026, 7, 1),
            data_leitura=date(2026, 7, 20),
            km=53_000,
        )


def test_km_menor_que_anterior_bloqueado(veiculo):
    registrar(veiculo, date(2026, 6, 10), 52_000)
    with pytest.raises(ValidationError):
        registrar(veiculo, date(2026, 7, 10), 51_000)


def test_registro_atualiza_km_atual_do_veiculo(veiculo):
    registrar(veiculo, date(2026, 7, 10), 52_000)
    veiculo.refresh_from_db()
    assert veiculo.km_atual == 52_000


def test_leituras_pendentes_do_mes(veiculo, db):
    Veiculo.objects.create(placa="RGD6H42", marca_modelo="Gol")
    Veiculo.objects.create(placa="RVZ9J95", marca_modelo="HB20", uso=Veiculo.Uso.FORA_LOCACAO)
    Veiculo.objects.create(placa="SWH9E89", marca_modelo="Virtus", status=Veiculo.Status.VENDIDO)
    registrar(veiculo, date(2026, 7, 10), 52_000)

    pendentes = veiculos_com_leitura_pendente(date(2026, 7, 25))
    assert list(pendentes.values_list("placa", flat=True)) == ["RGD6H42"]


def test_telas_de_km_renderizam(usuario_logado, veiculo):
    registrar(veiculo, date(2026, 7, 10), 52_000)
    assert usuario_logado.get("/km/").status_code == 200
    assert usuario_logado.get(f"/km/historico/{veiculo.pk}/").status_code == 200


def test_registrar_km_pela_tela(usuario_logado, veiculo):
    resposta = usuario_logado.post(
        f"/km/registrar/{veiculo.pk}/",
        {"data_leitura": "2026-07-15", "km": "52000"},
    )
    assert resposta.status_code == 302
    assert veiculo.registros_km.count() == 1


# --- Cadeia de leituras fora de ordem (revisão etapa 9) ---------------------


def test_leitura_retroativa_maior_que_a_do_mes_seguinte_e_bloqueada(veiculo):
    """Mês esquecido não pode entrar com odômetro maior que o já gravado adiante."""
    registrar(veiculo, date(2026, 8, 10), 62_000)
    with pytest.raises(ValidationError):
        registrar(veiculo, date(2026, 7, 11), 70_000)


def test_leitura_retroativa_reencadeia_o_mes_seguinte(veiculo):
    """Julho lançado depois de agosto: agosto passa a apoiar em julho."""
    agosto = registrar(veiculo, date(2026, 8, 10), 62_000)
    assert agosto.km_anterior == 50_000  # sem julho, a cadeia começa na compra

    julho = registrar(veiculo, date(2026, 7, 11), 56_000)

    agosto.refresh_from_db()
    assert agosto.km_anterior == 56_000
    assert agosto.dias == 30
    assert agosto.km_utilizado == 6_000
    # o km rodado deixa de ser contado duas vezes (ficha de desmobilização,
    # excedente de km e média/dia leem essa soma)
    assert julho.km_utilizado + agosto.km_utilizado == 62_000 - 50_000
    assert julho.seguinte_reencadeado == agosto


def test_editar_leitura_recalcula_o_registro_seguinte(veiculo):
    """Correção pelo admin: o mês seguinte volta a apoiar na leitura certa."""
    junho = registrar(veiculo, date(2026, 6, 10), 52_000)
    julho = registrar(veiculo, date(2026, 7, 10), 55_000)

    junho.km = 53_000  # tinha sido digitado 52.000 por engano
    junho.data_leitura = date(2026, 6, 20)
    junho.save()

    julho.refresh_from_db()
    assert julho.km_anterior == 53_000
    assert julho.dias == 20
    assert julho.km_utilizado == 2_000


def test_registro_retroativo_avisa_que_o_mes_seguinte_mudou(usuario_logado, veiculo):
    registrar(veiculo, date(2026, 8, 10), 62_000)
    resposta = usuario_logado.post(
        f"/km/registrar/{veiculo.pk}/",
        {"data_leitura": "2026-07-11", "km": "56000", "mes": "2026-07"},
        follow=True,
    )
    mensagens = [str(mensagem) for mensagem in resposta.context["messages"]]
    assert any("08/2026" in mensagem and "recalculada" in mensagem for mensagem in mensagens)


# --- Fechar um mês passado pela tela (revisão etapa 9) ----------------------


def test_data_sugerida_nunca_cai_fora_do_mes_visto():
    assert _data_sugerida(date(2026, 7, 1), date(2026, 8, 11)) == date(2026, 7, 31)
    assert _data_sugerida(date(2026, 2, 1), date(2026, 8, 11)) == date(2026, 2, 28)
    assert _data_sugerida(date(2026, 8, 1), date(2026, 8, 11)) == date(2026, 8, 11)  # mês corrente


def test_tela_de_mes_passado_sugere_data_do_proprio_mes(usuario_logado, veiculo):
    conteudo = usuario_logado.get("/km/?mes=2024-02").content.decode()
    assert 'value="2024-02-29"' in conteudo  # e não a data de hoje
    assert 'name="mes" value="2024-02"' in conteudo


# --- Cliente de cada leitura em lote (revisão de performance) ---------------


def _frota_com_leituras(quantidade, mes=date(2026, 7, 1)):
    """Veículos alocados com leitura no mês — cada linha mostra o cliente."""
    from decimal import Decimal

    from apps.alocacoes.models import Alocacao
    from apps.pessoas.models import Cliente

    for indice in range(quantidade):
        carro = Veiculo.objects.create(
            placa=f"TQ{indice:02d}A{indice:02d}", marca_modelo="Gol", km_compra=50_000
        )
        pessoa = Cliente.objects.create(
            nome=f"Motorista {indice}", cpf_cnpj=f"111.222.333-{indice:02d}"
        )
        Alocacao.objects.create(
            veiculo=carro,
            cliente=pessoa,
            data_inicio=mes,
            valor_semanal=Decimal("650.00"),
            dia_vencimento=mes.weekday(),
            km_entrega=50_000,
        )
        registrar(carro, mes.replace(day=10), 52_000 + indice)


def test_lista_mensal_resolve_o_cliente_em_lote(usuario_logado, django_assert_max_num_queries, db):
    """Eram 2 queries por linha (cliente_vigente por registro)."""
    _frota_com_leituras(12)
    with django_assert_max_num_queries(10):
        resposta = usuario_logado.get("/km/?mes=2026-07")
    assert resposta.status_code == 200
    conteudo = resposta.content.decode()
    for indice in range(12):
        assert f"Motorista {indice}" in conteudo


def test_lista_mensal_mostra_quem_estava_com_o_substituto(usuario_logado, veiculo, db):
    """A regra de cliente_vigente (troca temporária na frente da alocação) fica igual."""
    from decimal import Decimal

    from apps.alocacoes.models import Alocacao, TrocaTemporaria
    from apps.pessoas.models import Cliente

    substituto = Veiculo.objects.create(placa="RNB9J66", marca_modelo="Voyage", km_compra=40_000)
    dono_do_carro = Cliente.objects.create(nome="Arlen Titular", cpf_cnpj="111.222.333-44")
    alocacao = Alocacao.objects.create(
        veiculo=veiculo,
        cliente=dono_do_carro,
        data_inicio=date(2026, 6, 1),
        valor_semanal=Decimal("650.00"),
        dia_vencimento=0,
        km_entrega=50_000,
    )
    TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=date(2026, 7, 5),
        data_devolucao=date(2026, 7, 20),
        km_retirada=40_000,
    )
    registrar(substituto, date(2026, 7, 10), 41_000)  # durante o empréstimo
    registrar(veiculo, date(2026, 7, 10), 52_000)
    outro = Veiculo.objects.create(placa="SWH9E89", marca_modelo="HB20", km_compra=10_000)
    registrar(outro, date(2026, 7, 10), 11_000)  # sem alocação: fica sem cliente

    conteudo = usuario_logado.get("/km/?mes=2026-07").content.decode()
    assert conteudo.count("Arlen Titular") == 2  # carro principal e substituto
    assert "—" in conteudo  # o carro sem alocação continua sem cliente


def test_registrar_fecha_a_pendencia_do_mes_visto_e_volta_para_ele(usuario_logado, veiculo):
    resposta = usuario_logado.post(
        f"/km/registrar/{veiculo.pk}/",
        {"data_leitura": "2024-02-29", "km": "51000", "mes": "2024-02"},
    )
    assert resposta.url == "/km/?mes=2024-02"
    assert veiculo.registros_km.get().mes_referencia == date(2024, 2, 1)
    assert veiculo not in veiculos_com_leitura_pendente(date(2024, 2, 1))
