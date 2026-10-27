"""Etapa 17 — achados da navegação na frota (cadastro do carro, categorias, venda)."""

import io
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from PIL import Image

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.frota import alertas, desmobilizacao
from apps.frota.models import Categoria, Fornecedor, PagamentoIpva, Veiculo
from apps.manutencao.models import Manutencao
from apps.pessoas.models import Cliente

HOJE = timezone.localdate()


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(
        placa="QXQ6C10",
        marca_modelo="Gol",
        km_atual=95_000,
        data_aquisicao=date(2024, 7, 1),
        valor_compra=Decimal("42000.00"),
    )


def _dados_carro(**extra):
    dados = {
        "placa": "RNB9J66",
        "marca_modelo": "Voyage",
        "uso": "locacao",
        "chave_reserva": "sim",
        "km_atual": "",
    }
    dados.update(extra)
    return dados


def _foto(nome="carro.png"):
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(buffer, format="PNG")
    return SimpleUploadedFile(nome, buffer.getvalue(), content_type="image/png")


# ---------- KM com ponto de milhar ----------


def test_km_com_ponto_de_milhar_no_cadastro_nao_vira_mil_vezes_menor(usuario_logado, db):
    resposta = usuario_logado.post(
        "/frota/veiculo/novo/", _dados_carro(km_compra="35.000", km_atual="35.200")
    )
    assert resposta.status_code == 302
    carro = Veiculo.objects.get(placa="RNB9J66")
    assert carro.km_compra == 35_000
    assert carro.km_atual == 35_200


def test_km_em_branco_no_carro_novo_usa_o_da_compra(usuario_logado, db):
    usuario_logado.post("/frota/veiculo/novo/", _dados_carro(km_compra="48.900"))
    assert Veiculo.objects.get(placa="RNB9J66").km_atual == 48_900


def test_km_torto_no_cadastro_da_mensagem_simples(usuario_logado, db):
    resposta = usuario_logado.post("/frota/veiculo/novo/", _dados_carro(km_compra="12.5"))
    assert resposta.status_code == 200
    assert "Digite o KM só com números" in resposta.content.decode()
    assert not Veiculo.objects.exists()


def test_km_do_cadastro_abre_teclado_numerico(usuario_logado, db):
    html = usuario_logado.get("/frota/veiculo/novo/").content.decode()
    assert 'name="km_compra"' in html and 'inputmode="numeric"' in html
    assert 'type="number" name="km_compra"' not in html


def test_km_da_venda_com_ponto_torto_e_recusado(usuario_logado, veiculo):
    resposta = usuario_logado.post(
        f"/frota/veiculo/{veiculo.pk}/vender/",
        {"data": HOJE.isoformat(), "valor": "40.000,00", "km": "95.5"},
        follow=True,
    )
    veiculo.refresh_from_db()
    assert veiculo.status != Veiculo.Status.VENDIDO
    assert "Digite o KM só com números" in resposta.content.decode()


# ---------- placa ----------


def test_placa_em_formato_invalido_e_recusada(usuario_logado, db):
    resposta = usuario_logado.post("/frota/veiculo/novo/", _dados_carro(placa="AB1"))
    assert resposta.status_code == 200
    assert "Placa inválida — ex.: ABC1D23 ou ABC1234." in resposta.content.decode()
    assert not Veiculo.objects.exists()


def test_placa_antiga_e_mercosul_sao_aceitas(usuario_logado, db):
    for placa in ("abc-1234", "ABC1D23"):
        resposta = usuario_logado.post("/frota/veiculo/novo/", _dados_carro(placa=placa))
        assert resposta.status_code == 302, placa
    assert set(Veiculo.objects.values_list("placa", flat=True)) == {"ABC1234", "ABC1D23"}


def test_placa_repetida_avisa_com_link_para_o_carro(usuario_logado, veiculo):
    resposta = usuario_logado.post("/frota/veiculo/novo/", _dados_carro(placa="qxq-6c10"))
    html = resposta.content.decode()
    assert "Já existe um carro com a placa QXQ6C10" in html
    assert f'href="/frota/veiculo/{veiculo.pk}/"' in html
    assert Veiculo.objects.count() == 1


def test_carro_antigo_com_placa_fora_do_padrao_continua_editavel(usuario_logado, db):
    antigo = Veiculo.objects.create(placa="XYZ12", marca_modelo="Uno", km_atual=1_000)
    resposta = usuario_logado.post(
        f"/frota/veiculo/{antigo.pk}/editar/",
        _dados_carro(placa="XYZ12", marca_modelo="Uno Way", km_atual="1.000"),
    )
    assert resposta.status_code == 302
    antigo.refresh_from_db()
    assert antigo.marca_modelo == "Uno Way"


def test_campo_placa_com_maiusculas_e_limite(usuario_logado, db):
    html = usuario_logado.get("/frota/veiculo/novo/").content.decode()
    assert 'autocapitalize="characters"' in html and 'maxlength="8"' in html


# ---------- categorias e fornecedores ----------


def test_categoria_repetida_mudando_so_maiuscula_e_recusada(usuario_logado, db):
    Categoria.objects.create(nome="Picape Teste", valor_semanal_referencia=Decimal("650.00"))
    antes = Categoria.objects.count()
    resposta = usuario_logado.post(
        "/frota/categorias/",
        {"nome": "  picape   TESTE ", "valor_semanal_referencia": "700,00", "observacoes": ""},
        follow=True,
    )
    assert "Já existe a categoria Picape Teste" in resposta.content.decode()
    assert Categoria.objects.count() == antes


def test_categoria_pode_mudar_a_propria_caixa(usuario_logado, db):
    categoria = Categoria.objects.create(nome="picape teste", valor_semanal_referencia=650)
    usuario_logado.post(
        "/frota/categorias/",
        {"categoria_id": categoria.pk, "nome": "Picape Teste", "valor_semanal_referencia": "650"},
    )
    categoria.refresh_from_db()
    assert categoria.nome == "Picape Teste"


def test_fornecedor_repetido_mudando_so_maiuscula_e_recusado(usuario_logado, db):
    Fornecedor.objects.create(nome="By Car")
    resposta = usuario_logado.post("/frota/fornecedores/", {"nome": "BY CAR"}, follow=True)
    assert "Já existe o fornecedor By Car" in resposta.content.decode()
    assert Fornecedor.objects.count() == 1


def test_categoria_com_valor_zero_e_recusada(usuario_logado, db):
    resposta = usuario_logado.post(
        "/frota/categorias/",
        {"nome": "Categoria Zero", "valor_semanal_referencia": "0,00"},
        follow=True,
    )
    assert "Informe um valor maior que zero" in resposta.content.decode()
    assert not Categoria.objects.filter(nome="Categoria Zero").exists()


def test_categorias_e_fornecedores_em_quadros_com_salvar_em_cada_um(usuario_logado, db):
    categoria = Categoria.objects.create(nome="Picape Teste", valor_semanal_referencia=650)
    fornecedor = Fornecedor.objects.create(nome="By Car")
    html = usuario_logado.get("/frota/categorias/").content.decode()
    assert "<table" not in html
    assert f'id="categoria-{categoria.pk}"' in html and "Valor semanal (R$)" in html
    html = usuario_logado.get("/frota/fornecedores/").content.decode()
    assert "<table" not in html
    assert f'id="fornecedor-{fornecedor.pk}"' in html and "Tipo de serviço" in html


# ---------- IPVA ----------


def test_ipva_com_valor_zero_e_recusado(usuario_logado, veiculo):
    resposta = usuario_logado.post(
        f"/frota/veiculo/{veiculo.pk}/ipva/",
        {
            "pagamentos_ipva-TOTAL_FORMS": "1",
            "pagamentos_ipva-INITIAL_FORMS": "0",
            "pagamentos_ipva-0-ano": str(HOJE.year),
            "pagamentos_ipva-0-parcela": "1",
            "pagamentos_ipva-0-valor": "0,00",
        },
    )
    assert resposta.status_code == 200
    assert "Informe um valor maior que zero" in resposta.content.decode()
    assert not PagamentoIpva.objects.exists()


# ---------- linha do tempo do carro substituto ----------


def test_card_linha_do_tempo_conta_trocas_do_substituto(usuario_logado, veiculo):
    substituto = Veiculo.objects.create(placa="RNB9J66", marca_modelo="Voyage", km_atual=50_000)
    alocacao = Alocacao.objects.create(
        veiculo=veiculo,
        cliente=Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44"),
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=95_000,
    )
    TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=date(2026, 7, 10),
        km_retirada=50_000,
    )
    resposta = usuario_logado.get(f"/frota/veiculo/{substituto.pk}/")
    assert resposta.context["total_alocacoes"] == 1


# ---------- foto e CRLV ----------


def test_trocar_foto_marcando_remover_e_escolhendo_outra_troca(usuario_logado, veiculo):
    veiculo.foto = _foto("antiga.png")
    veiculo.save()
    html = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/editar/").content.decode()
    assert "Ver arquivo atual" in html and "Atualmente" not in html
    resposta = usuario_logado.post(
        f"/frota/veiculo/{veiculo.pk}/editar/",
        _dados_carro(placa="QXQ6C10", marca_modelo="Gol", km_atual="95000", foto=_foto("nova.png"))
        | {"foto-clear": "on"},
    )
    assert resposta.status_code == 302
    veiculo.refresh_from_db()
    assert "nova" in veiculo.foto.name


def test_crlv_com_extensao_errada_explica_o_que_enviar(usuario_logado, veiculo):
    arquivo = SimpleUploadedFile("crlv.docx", b"conteudo", content_type="application/msword")
    resposta = usuario_logado.post(
        f"/frota/veiculo/{veiculo.pk}/editar/",
        _dados_carro(placa="QXQ6C10", marca_modelo="Gol", km_atual="95000", documento=arquivo),
    )
    assert resposta.status_code == 200
    assert "Envie foto (JPG/PNG) ou PDF do documento." in resposta.content.decode()


def test_foto_que_nao_e_imagem_explica_o_que_enviar(usuario_logado, veiculo):
    arquivo = SimpleUploadedFile("carro.pdf", b"%PDF-1.4", content_type="application/pdf")
    resposta = usuario_logado.post(
        f"/frota/veiculo/{veiculo.pk}/editar/",
        _dados_carro(placa="QXQ6C10", marca_modelo="Gol", km_atual="95000", foto=arquivo),
    )
    assert resposta.status_code == 200
    assert "Envie uma foto do carro (JPG ou PNG)." in resposta.content.decode()


# ---------- quando vender ----------


def test_resultado_negativo_mostra_ainda_nao_voltou_com_o_prejuizo(usuario_logado, veiculo):
    Manutencao.objects.create(
        veiculo=veiculo,
        tipo=Manutencao.Tipo.CORRETIVA,
        data=HOJE,
        descricao="Motor",
        custo_real=Decimal("3780.00"),
    )
    assert desmobilizacao.montar_ficha(veiculo).prejuizo_operacional == Decimal("3780.00")
    for url in (
        f"/frota/veiculo/{veiculo.pk}/ficha/",
        f"/frota/veiculo/{veiculo.pk}/vender/",
        "/frota/desmobilizacao/",
    ):
        html = usuario_logado.get(url).content.decode()
        assert "ainda não voltou" in html, url
        assert "-9%" not in html, url


def test_ficha_mostra_o_motivo_ao_lado_do_selo(usuario_logado, veiculo):
    ficha = desmobilizacao.FichaFinanceira(veiculo=veiculo)
    ficha.motivos = ["5 manutenções esporádicas pesadas nos últimos 6 meses"]
    ficha.nivel = "observar"
    from unittest import mock

    with mock.patch.object(desmobilizacao, "avaliar", return_value=ficha):
        html = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/ficha/").content.decode()
    assert "Por quê: 5 manutenções esporádicas pesadas nos últimos 6 meses." in html


# ---------- dias na oficina ----------


def _na_oficina(veiculo, dias):
    return Manutencao.objects.create(
        veiculo=veiculo,
        tipo=Manutencao.Tipo.CORRETIVA,
        data=HOJE - timedelta(days=dias),
        descricao="Embreagem",
        data_entrada=HOJE - timedelta(days=dias),
    )


@pytest.mark.parametrize(
    "dias,texto", [(0, "Na oficina desde hoje"), (1, "Na oficina desde ontem")]
)
def test_oficina_sem_zero_dias(usuario_logado, veiculo, dias, texto):
    _na_oficina(veiculo, dias)
    for url in ("/frota/", f"/frota/veiculo/{veiculo.pk}/"):
        html = usuario_logado.get(url).content.decode()
        assert texto in html, url
        assert "(0 dias)" not in html and "(1 dia)" not in html, url
    preventivas = usuario_logado.get("/manutencao/preventivas/").content.decode()
    assert "desde hoje" in preventivas or "desde ontem" in preventivas


def test_pendencia_da_oficina_no_dia_da_entrada(veiculo):
    _na_oficina(veiculo, 0)
    textos = " ".join(str(p) for p in alertas.pendencias_do_veiculo(veiculo))
    assert "0 dias" not in textos


# ---------- revisão adversarial ----------


def test_prejuizo_na_ficha_tem_cor_legivel_no_fundo_preto(usuario_logado, veiculo):
    Manutencao.objects.create(
        veiculo=veiculo,
        tipo=Manutencao.Tipo.CORRETIVA,
        data=HOJE,
        descricao="Motor",
        custo_real=Decimal("500.00"),
    )
    html = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/ficha/").content.decode()
    # o cartão é preto: o vermelho escuro some; aqui vai o vermelho claro
    assert "text-red-300" in html and "(prejuízo de R$" in html


def test_ficha_nao_repete_os_motivos_em_duas_listas(usuario_logado, veiculo):
    from unittest import mock

    ficha = desmobilizacao.FichaFinanceira(veiculo=veiculo)
    ficha.motivos = ["30 dias parado em oficina nos últimos 6 meses"]
    ficha.nivel = "observar"
    with mock.patch.object(desmobilizacao, "avaliar", return_value=ficha):
        html = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/ficha/").content.decode()
    assert html.count("30 dias parado em oficina") == 1


def test_placa_repetida_digitada_com_hifen_e_minuscula_tambem_avisa(usuario_logado, veiculo):
    resposta = usuario_logado.post("/frota/veiculo/novo/", _dados_carro(placa="qxq-6c10"))
    html = resposta.content.decode()
    assert resposta.status_code == 200
    assert "Já existe um carro com a placa QXQ6C10" in html
    assert f'href="/frota/veiculo/{veiculo.pk}/"' in html
    assert Veiculo.objects.filter(placa="QXQ6C10").count() == 1


def test_km_gigante_no_cadastro_e_recusado_sem_erro_500(usuario_logado, db):
    resposta = usuario_logado.post("/frota/veiculo/novo/", _dados_carro(km_compra="99.999.999.999"))
    assert resposta.status_code == 200
    assert "grande demais" in resposta.content.decode()
    assert not Veiculo.objects.exists()


def test_editar_carro_mostra_o_km_sem_perder_o_valor(usuario_logado, veiculo):
    html = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/editar/").content.decode()
    assert 'value="95000"' in html
    resposta = usuario_logado.post(
        f"/frota/veiculo/{veiculo.pk}/editar/",
        _dados_carro(placa="QXQ6C10", marca_modelo="Gol", km_atual="95.000"),
    )
    assert resposta.status_code == 302
    veiculo.refresh_from_db()
    assert veiculo.km_atual == 95_000


def test_km_da_venda_com_ponto_de_milhar_grava_certo(usuario_logado, veiculo):
    usuario_logado.post(
        f"/frota/veiculo/{veiculo.pk}/vender/",
        {"data": HOJE.isoformat(), "valor": "40.000,00", "km": "96.500"},
    )
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.VENDIDO
    assert veiculo.km_atual == 96_500


def test_categoria_editada_para_nome_de_outra_com_espacos_e_recusada(usuario_logado, db):
    Categoria.objects.create(nome="Sedan", valor_semanal_referencia=Decimal("700"))
    hatch = Categoria.objects.create(nome="Hatch", valor_semanal_referencia=Decimal("600"))
    resposta = usuario_logado.post(
        "/frota/categorias/",
        {"categoria_id": hatch.pk, "nome": "  sedan ", "valor_semanal_referencia": "600,00"},
        follow=True,
    )
    assert "Já existe a categoria Sedan" in resposta.content.decode()
    hatch.refresh_from_db()
    assert hatch.nome == "Hatch"


def test_linha_do_tempo_nao_conta_duas_vezes_a_mesma_alocacao(usuario_logado, veiculo):
    alocacao = Alocacao.objects.create(
        veiculo=veiculo,
        cliente=Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44"),
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=95_000,
    )
    # duas trocas com carros diferentes: o JOIN não pode duplicar a alocação do titular
    for placa, dia in (("RNB9J66", 5), ("RNB9J67", 20)):
        TrocaTemporaria.objects.create(
            alocacao=alocacao,
            veiculo_substituto=Veiculo.objects.create(
                placa=placa, marca_modelo="Voyage", km_atual=50_000
            ),
            data_retirada=date(2026, 7, dia),
            data_devolucao=date(2026, 7, dia + 2),
            km_retirada=50_000,
            km_devolucao=50_100,
        )
    resposta = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/")
    assert resposta.context["total_alocacoes"] == 1
