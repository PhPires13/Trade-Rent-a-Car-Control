from datetime import date
from decimal import Decimal

import pytest

from apps.alocacoes.models import Alocacao
from apps.financeiro.models import Cobranca
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente, CondutorAutorizado


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(
        nome="Arlen Souza",
        cpf_cnpj="111.222.333-44",
        telefone="(21) 98888-7777",
        cnh_numero="123456789",
        cnh_validade=date(2030, 1, 1),
    )


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(
        placa="QXQ6C10",
        marca_modelo="Gol",
        km_atual=95_000,
        data_aquisicao=date(2024, 7, 1),
        valor_compra=Decimal("42000.00"),
    )


@pytest.fixture
def alocacao(cliente, veiculo):
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=95_000,
    )


def test_lista_mostra_carro_atual_e_saldo_devedor(usuario_logado, alocacao, cliente):
    Cobranca.objects.create(
        cliente=cliente,
        alocacao=alocacao,
        origem=Cobranca.Origem.ALUGUEL,
        descricao="Aluguel semanal",
        valor=Decimal("650.00"),
        vencimento=date(2026, 7, 8),
    )
    resposta = usuario_logado.get("/clientes/")
    assert resposta.status_code == 200
    conteudo = resposta.content.decode()
    assert "Arlen Souza" in conteudo
    assert "QXQ6C10" in conteudo
    assert "650" in conteudo


def test_lista_esconde_inativos_e_filtra_por_status(usuario_logado, cliente):
    Cliente.objects.create(
        nome="Welington Antigo", cpf_cnpj="555.666.777-88", status=Cliente.Status.INATIVO
    )
    padrao = usuario_logado.get("/clientes/").content.decode()
    assert "Welington Antigo" not in padrao
    assert "Arlen Souza" in padrao

    filtrado = usuario_logado.get("/clientes/?status=inativo").content.decode()
    assert "Welington Antigo" in filtrado
    assert "Arlen Souza" not in filtrado


def test_lista_filtra_por_nome(usuario_logado, cliente):
    Cliente.objects.create(nome="Bruno Lima", cpf_cnpj="999.888.777-66")
    conteudo = usuario_logado.get("/clientes/?q=bruno").content.decode()
    assert "Bruno Lima" in conteudo
    assert "Arlen Souza" not in conteudo


def test_link_whatsapp_usa_somente_digitos(usuario_logado, cliente):
    conteudo = usuario_logado.get("/clientes/").content.decode()
    assert "https://wa.me/5521988887777" in conteudo


def test_detalhe_mostra_alocacao_ativa_multas_e_condutores(usuario_logado, alocacao, cliente):
    CondutorAutorizado.objects.create(nome="Maria Condutora", cpf="000.111.222-33", cliente=cliente)
    resposta = usuario_logado.get(f"/clientes/{cliente.pk}/")
    assert resposta.status_code == 200
    conteudo = resposta.content.decode()
    assert "QXQ6C10" in conteudo
    assert "650" in conteudo
    assert "Maria Condutora" in conteudo
    assert "https://wa.me/5521988887777" in conteudo


def test_criar_cliente_pela_tela(usuario_logado, db):
    dados = {
        "nome": "Novo Cliente",
        "cpf_cnpj": "123.456.789-00",
        "telefone": "21999990000",
        "email": "",
        "endereco": "",
        "cnh_numero": "",
        "cnh_categoria": "",
        "cnh_validade": "",
        "dia_vencimento": "",
        "caucao_referencia": "",
        "status": Cliente.Status.ATIVO,
        "observacoes": "",
    }
    resposta = usuario_logado.post("/clientes/novo/", dados)
    assert resposta.status_code == 302
    criado = Cliente.objects.get(cpf_cnpj="123.456.789-00")
    assert resposta.url == f"/clientes/{criado.pk}/"


def test_cpf_repetido_volta_com_erro_no_form(usuario_logado, cliente):
    dados = {
        "nome": "Homônimo",
        "cpf_cnpj": cliente.cpf_cnpj,
        "telefone": "",
        "email": "",
        "endereco": "",
        "cnh_numero": "",
        "cnh_categoria": "",
        "cnh_validade": "",
        "dia_vencimento": "",
        "caucao_referencia": "",
        "status": Cliente.Status.ATIVO,
        "observacoes": "",
    }
    resposta = usuario_logado.post("/clientes/novo/", dados)
    assert resposta.status_code == 200
    assert Cliente.objects.filter(nome="Homônimo").count() == 0
    assert resposta.context["form"].errors["cpf_cnpj"]


def test_editar_cliente_pela_tela(usuario_logado, cliente):
    dados = {
        "nome": "Arlen Souza",
        "cpf_cnpj": cliente.cpf_cnpj,
        "telefone": "21970000000",
        "email": "arlen@example.com",
        "endereco": "",
        "cnh_numero": cliente.cnh_numero,
        "cnh_categoria": "B",
        "cnh_validade": "2030-01-01",
        "dia_vencimento": Cliente.DiaSemana.SEXTA,
        "caucao_referencia": "500.00",
        "status": Cliente.Status.ATIVO,
        "observacoes": "",
    }
    resposta = usuario_logado.post(f"/clientes/{cliente.pk}/editar/", dados)
    assert resposta.status_code == 302
    cliente.refresh_from_db()
    assert cliente.email == "arlen@example.com"
    assert cliente.dia_vencimento == Cliente.DiaSemana.SEXTA


def test_inadimplente_nao_e_opcao_manual(usuario_logado, cliente):
    """Revisão etapa 8: a rotina diária reverteria a marcação manual à noite."""
    dados = {
        "nome": cliente.nome,
        "cpf_cnpj": cliente.cpf_cnpj,
        "status": Cliente.Status.INADIMPLENTE,
    }
    resposta = usuario_logado.post(f"/clientes/{cliente.pk}/editar/", dados)
    assert resposta.status_code == 200  # volta com erro de validação no campo
    cliente.refresh_from_db()
    assert cliente.status != Cliente.Status.INADIMPLENTE


def test_adicionar_condutor_pelo_form_inline(usuario_logado, cliente):
    resposta = usuario_logado.post(
        f"/clientes/{cliente.pk}/condutores/novo/",
        {"nome": "Maria Condutora", "cpf": "000.111.222-33", "contato": "21988880000"},
    )
    assert resposta.status_code == 302
    assert resposta.url == f"/clientes/{cliente.pk}/"
    condutor = CondutorAutorizado.objects.get(nome="Maria Condutora")
    assert condutor.cliente == cliente


def test_editar_condutor_volta_para_o_cliente(usuario_logado, cliente):
    condutor = CondutorAutorizado.objects.create(nome="Maria", cliente=cliente)
    assert usuario_logado.get(f"/clientes/condutores/{condutor.pk}/editar/").status_code == 200
    resposta = usuario_logado.post(
        f"/clientes/condutores/{condutor.pk}/editar/",
        {
            "nome": "Maria Condutora",
            "cpf": "000.111.222-33",
            "cnh_numero": "987654321",
            "contato": "21988880000",
            "observacoes": "",
        },
    )
    assert resposta.status_code == 302
    assert resposta.url == f"/clientes/{cliente.pk}/"
    condutor.refresh_from_db()
    assert condutor.nome == "Maria Condutora"
    assert condutor.cnh_numero == "987654321"
