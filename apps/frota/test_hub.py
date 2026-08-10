from datetime import date
from decimal import Decimal

import pytest

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.frota.models import Categoria, Fornecedor, Veiculo
from apps.pessoas.models import Cliente


@pytest.fixture
def usuario_logado(client, django_user_model):
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    client.login(username="dono", password="senha-forte-123")
    return client


@pytest.fixture
def categoria(db):
    return Categoria.objects.create(nome="Econômico", valor_semanal_referencia=Decimal("650.00"))


@pytest.fixture
def veiculo(db, categoria):
    return Veiculo.objects.create(
        placa="QXQ6C10",
        marca_modelo="Gol",
        ano="20/21",
        categoria=categoria,
        km_atual=95_000,
        data_aquisicao=date(2024, 7, 1),
        valor_compra=Decimal("42000.00"),
    )


@pytest.fixture
def substituto(db):
    return Veiculo.objects.create(placa="RNB9J66", marca_modelo="Voyage", km_atual=50_000)


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


@pytest.fixture
def alocacao(veiculo, cliente):
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=95_000,
    )


def test_hub_lista_cards_com_motorista_atual(usuario_logado, alocacao, veiculo, cliente):
    resposta = usuario_logado.get("/frota/")
    assert resposta.status_code == 200
    conteudo = resposta.content.decode()
    assert "QXQ6C10" in conteudo
    assert "Arlen" in conteudo
    assert "Alocado" in conteudo


def test_hub_mostra_cliente_do_carro_substituto(usuario_logado, alocacao, substituto, cliente):
    TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=date(2026, 7, 10),
        km_retirada=50_000,
    )
    conteudo = usuario_logado.get("/frota/").content.decode()
    assert "RNB9J66" in conteudo
    assert "Arlen (substituto)" in conteudo


def test_hub_sem_motorista_quando_disponivel(usuario_logado, veiculo):
    conteudo = usuario_logado.get("/frota/").content.decode()
    assert "Sem motorista no momento" in conteudo


def test_hub_filtra_por_status(usuario_logado, veiculo, substituto):
    substituto.status = Veiculo.Status.EM_MANUTENCAO
    substituto.save()
    conteudo = usuario_logado.get("/frota/", {"status": "em_manutencao"}).content.decode()
    assert "RNB9J66" in conteudo
    assert "QXQ6C10" not in conteudo


def test_hub_filtra_por_uso(usuario_logado, veiculo, substituto):
    substituto.uso = Veiculo.Uso.FORA_LOCACAO
    substituto.save()
    conteudo = usuario_logado.get("/frota/", {"uso": "fora_locacao"}).content.decode()
    assert "RNB9J66" in conteudo
    assert "Fora de locação" in conteudo
    assert "QXQ6C10" not in conteudo


def test_hub_busca_por_placa_normaliza_maiusculas(usuario_logado, veiculo, substituto):
    conteudo = usuario_logado.get("/frota/", {"placa": "qxq6c"}).content.decode()
    assert "QXQ6C10" in conteudo
    assert "RNB9J66" not in conteudo


def test_hub_esconde_vendidos_ate_serem_filtrados(usuario_logado, veiculo, substituto):
    substituto.status = Veiculo.Status.VENDIDO
    substituto.save()
    padrao = usuario_logado.get("/frota/").content.decode()
    assert "RNB9J66" not in padrao
    com_vendidos = usuario_logado.get("/frota/", {"status": "todos"}).content.decode()
    assert "RNB9J66" in com_vendidos


def test_detalhe_renderiza_com_contagens_e_atalhos(usuario_logado, alocacao, veiculo):
    resposta = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/")
    assert resposta.status_code == 200
    conteudo = resposta.content.decode()
    assert "QXQ6C10" in conteudo
    assert "Arlen" in conteudo
    assert "Dados cadastrais" in conteudo
    assert f"/frota/veiculo/{veiculo.pk}/ficha/" in conteudo
    assert f"/frota/veiculo/{veiculo.pk}/editar/" in conteudo
    assert "?veiculo=QXQ6C10" in conteudo


def test_detalhe_esconde_alocar_quando_nao_disponivel(usuario_logado, alocacao, veiculo):
    conteudo = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/").content.decode()
    assert ">Alocar<" not in conteudo


def test_criar_veiculo_pela_tela_normaliza_a_placa(usuario_logado, categoria):
    assert usuario_logado.get("/frota/veiculo/novo/").status_code == 200
    resposta = usuario_logado.post(
        "/frota/veiculo/novo/",
        {
            "placa": "rnb-9j66",
            "marca_modelo": "Voyage",
            "ano": "21/22",
            "categoria": categoria.pk,
            "uso": "locacao",
            "status": "disponivel",
            "km_atual": "12000",
            "chave_reserva": "sim",
            "valor_compra": "48000.00",
            "data_aquisicao": "2026-01-10",
        },
    )
    assert resposta.status_code == 302
    veiculo = Veiculo.objects.get(placa="RNB9J66")
    assert veiculo.marca_modelo == "Voyage"
    assert veiculo.categoria == categoria
    assert resposta.url == f"/frota/veiculo/{veiculo.pk}/"


def test_editar_veiculo_pela_tela(usuario_logado, veiculo):
    assert usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/editar/").status_code == 200
    resposta = usuario_logado.post(
        f"/frota/veiculo/{veiculo.pk}/editar/",
        {
            "placa": "QXQ6C10",
            "marca_modelo": "Gol 1.0",
            "ano": "20/21",
            "uso": "locacao",
            "status": "disponivel",
            "km_atual": "98000",
            "chave_reserva": "duvida",
            "observacoes": "Revisão feita",
        },
    )
    assert resposta.status_code == 302
    veiculo.refresh_from_db()
    assert veiculo.marca_modelo == "Gol 1.0"
    assert veiculo.km_atual == 98_000
    assert Veiculo.objects.count() == 1


def test_categorias_cria_e_edita(usuario_logado, categoria, veiculo):
    resposta = usuario_logado.get("/frota/categorias/")
    assert resposta.status_code == 200
    assert "Econômico" in resposta.content.decode()

    criar = usuario_logado.post(
        "/frota/categorias/",
        {"nome": "Executivo", "valor_semanal_referencia": "900.00", "observacoes": ""},
    )
    assert criar.status_code == 302
    assert Categoria.objects.filter(nome="Executivo").exists()

    editar = usuario_logado.post(
        "/frota/categorias/",
        {
            "categoria_id": categoria.pk,
            "nome": "Econômico plus",
            "valor_semanal_referencia": "700.00",
            "observacoes": "reajuste",
        },
    )
    assert editar.status_code == 302
    categoria.refresh_from_db()
    assert categoria.nome == "Econômico plus"
    assert categoria.valor_semanal_referencia == Decimal("700.00")


def test_fornecedores_cria_e_edita(usuario_logado, db):
    fornecedor = Fornecedor.objects.create(nome="By Car", tipo_servico="mecânica")
    resposta = usuario_logado.get("/frota/fornecedores/")
    assert resposta.status_code == 200
    assert "By Car" in resposta.content.decode()

    criar = usuario_logado.post(
        "/frota/fornecedores/",
        {
            "nome": "Pedrinho Baterias",
            "cnpj": "",
            "contato": "",
            "tipo_servico": "bateria",
            "observacoes": "",
        },
    )
    assert criar.status_code == 302
    assert Fornecedor.objects.filter(nome="Pedrinho Baterias").exists()

    editar = usuario_logado.post(
        "/frota/fornecedores/",
        {
            "fornecedor_id": fornecedor.pk,
            "nome": "By Car Funilaria",
            "cnpj": "12.345.678/0001-90",
            "contato": "11 99999-0000",
            "tipo_servico": "funilaria",
            "observacoes": "",
        },
    )
    assert editar.status_code == 302
    fornecedor.refresh_from_db()
    assert fornecedor.nome == "By Car Funilaria"
    assert fornecedor.tipo_servico == "funilaria"
