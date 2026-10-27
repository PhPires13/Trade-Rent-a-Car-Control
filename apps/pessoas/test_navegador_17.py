"""Etapa 17 (testes no navegador) — clientes e condutores: CPF/CNPJ conferido e
num formato só (sem duplicado), CNH com 11 números, telefone padronizado e só
conferido quando muda, troca de arquivo sem erro técnico e textos da ficha."""

import importlib
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.pessoas import validacao
from apps.pessoas.models import Cliente, CondutorAutorizado
from apps.pessoas.views import ClienteForm, CondutorForm, CondutorInlineForm

HOJE = timezone.localdate()


@pytest.fixture(autouse=True)
def midia_temporaria(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen Souza", cpf_cnpj="529.982.247-25")


def _dados(**extra):
    return {"nome": "Bruna Lima", "cpf_cnpj": "111.444.777-35", **extra}


# --- CPF/CNPJ -----------------------------------------------------------------


@pytest.mark.parametrize(
    "texto, esperado",
    [
        ("52998224725", "529.982.247-25"),
        ("529.982.247-25", "529.982.247-25"),
        ("11222333000181", "11.222.333/0001-81"),
    ],
)
def test_cpf_cnpj_valido_fica_num_formato_so(texto, esperado):
    assert validacao.cpf_cnpj(texto) == esperado


@pytest.mark.parametrize("texto", ["111", "123", "abc", "111.111.111-11", "529.982.247-26"])
def test_cpf_cnpj_invalido_e_recusado(db, texto):
    form = ClienteForm(data=_dados(cpf_cnpj=texto))
    assert not form.is_valid()
    assert "CPF ou CNPJ inválido" in form.errors["cpf_cnpj"][0]


def test_mesmo_cpf_em_outro_formato_nao_cria_duplicado(usuario_logado, cliente):
    resposta = usuario_logado.post("/clientes/novo/", _dados(cpf_cnpj="52998224725"))
    html = resposta.content.decode()
    assert resposta.status_code == 200
    assert Cliente.objects.count() == 1
    assert "Já existe o cliente Arlen Souza com este CPF/CNPJ" in html
    assert f'href="/clientes/{cliente.pk}/"' in html


def test_duplicado_achado_mesmo_com_cadastro_antigo_sem_pontos(db):
    Cliente.objects.create(nome="Antigo", cpf_cnpj="52998224725")
    form = ClienteForm(data=_dados(cpf_cnpj="529.982.247-25"))
    assert not form.is_valid()
    assert form.cliente_existente.nome == "Antigo"


def test_cadastro_novo_grava_cpf_formatado(usuario_logado):
    resposta = usuario_logado.post("/clientes/novo/", _dados(cpf_cnpj="11144477735"))
    assert resposta.status_code == 302
    assert Cliente.objects.get().cpf_cnpj == "111.444.777-35"


def test_migracao_normaliza_cpf_antigo_sem_criar_conflito(db):
    migracao = importlib.import_module("apps.pessoas.migrations.0004_normalizar_cpf_cnpj")
    Cliente.objects.create(nome="A", cpf_cnpj="52998224725")
    Cliente.objects.create(nome="B", cpf_cnpj="11144477735")
    Cliente.objects.create(nome="C", cpf_cnpj="111.444.777-35")  # mesmo CPF do B já formatado
    Cliente.objects.create(nome="D", cpf_cnpj="123")

    from django.apps import apps

    migracao.normalizar(apps, None)
    cpfs = dict(Cliente.objects.values_list("nome", "cpf_cnpj"))
    assert cpfs == {
        "A": "529.982.247-25",
        "B": "11144477735",  # ficaria igual ao C: fica como está para juntar à mão
        "C": "111.444.777-35",
        "D": "123",
    }


def test_editar_outro_campo_nao_trava_por_cpf_antigo(db):
    antigo = Cliente.objects.create(nome="Antigo", cpf_cnpj="123")
    form = ClienteForm(
        data={"nome": "Antigo da Silva", "cpf_cnpj": "123", "status": "ativo"},
        instance=antigo,
    )
    assert form.is_valid(), form.errors
    assert form.save().nome == "Antigo da Silva"


# --- CNH ----------------------------------------------------------------------


@pytest.mark.parametrize("texto", ["123", "abc", "1234567890a", "1" * 12])
def test_cnh_do_cliente_precisa_de_11_numeros(db, texto):
    form = ClienteForm(data=_dados(cnh_numero=texto))
    assert not form.is_valid()
    assert "11 números" in form.errors["cnh_numero"][0]


def test_categoria_da_cnh_em_maiusculas(db):
    form = ClienteForm(data=_dados(cnh_numero="123.456.789-00", cnh_categoria=" ab "))
    assert form.is_valid(), form.errors
    cliente = form.save()
    assert cliente.cnh_categoria == "AB"
    assert cliente.cnh_numero == "12345678900"


def test_cnh_vencida_avisa_ao_salvar(usuario_logado):
    vencida = HOJE - timedelta(days=10)
    resposta = usuario_logado.post(
        "/clientes/novo/", _dados(cnh_validade=vencida.isoformat()), follow=True
    )
    assert f"venceu em {vencida:%d/%m/%Y}" in resposta.content.decode()


def test_ficha_sem_numero_da_cnh_diz_sem_numero(usuario_logado, cliente):
    cliente.cnh_validade = HOJE + timedelta(days=300)
    cliente.save()
    html = usuario_logado.get(f"/clientes/{cliente.pk}/").content.decode()
    assert "sem número" in html


# --- condutor -----------------------------------------------------------------


@pytest.mark.parametrize("campo, valor", [("cpf", "111"), ("cpf", "abc"), ("cnh_numero", "123")])
def test_condutor_com_documento_invalido_e_recusado(db, campo, valor):
    form = CondutorInlineForm(data={"nome": "Rita", campo: valor})
    assert not form.is_valid()
    assert campo in form.errors


def test_condutor_sem_documentos_continua_valendo(db):
    assert CondutorInlineForm(data={"nome": "Rita"}).is_valid()


def test_condutor_grava_cpf_formatado(db):
    form = CondutorInlineForm(data={"nome": "Rita", "cpf": "11144477735"})
    assert form.is_valid()
    assert form.cleaned_data["cpf"] == "111.444.777-35"


def test_editar_condutor_nao_repete_nome_de_outro(cliente):
    CondutorAutorizado.objects.create(nome="Maria José", cliente=cliente)
    outro = CondutorAutorizado.objects.create(nome="Rita", cliente=cliente)
    form = CondutorForm(data={"nome": "maria jose"}, instance=outro)
    assert not form.is_valid()
    assert "já é condutor" in form.errors["nome"][0]
    # o próprio nome (sem mudar) continua aceito
    assert CondutorForm(data={"nome": "Rita"}, instance=outro).is_valid()


# --- telefone -----------------------------------------------------------------


def test_telefone_e_padronizado(db):
    form = ClienteForm(data=_dados(telefone="+55 31 988887777"))
    assert form.is_valid(), form.errors
    assert form.save().telefone == "(31) 98888-7777"


def test_telefone_antigo_sem_ddd_nao_impede_salvar_outra_mudanca(usuario_logado, cliente):
    cliente.telefone = "98888-7777"
    cliente.save()
    resposta = usuario_logado.post(
        f"/clientes/{cliente.pk}/editar/",
        {
            "nome": "Arlen Souza Lima",
            "cpf_cnpj": cliente.cpf_cnpj,
            "telefone": "98888-7777",
            "status": "ativo",
        },
    )
    assert resposta.status_code == 302
    cliente.refresh_from_db()
    assert cliente.nome == "Arlen Souza Lima"
    html = usuario_logado.get(f"/clientes/{cliente.pk}/").content.decode()
    assert "telefone sem DDD" in html


def test_telefone_alterado_sem_ddd_continua_recusado(cliente):
    form = ClienteForm(
        data={"nome": cliente.nome, "cpf_cnpj": cliente.cpf_cnpj, "telefone": "98888-7777"},
        instance=cliente,
    )
    assert not form.is_valid()
    assert "DDD" in form.errors["telefone"][0]


def test_teclado_certo_no_celular(usuario_logado):
    html = usuario_logado.get("/clientes/novo/").content.decode()
    assert 'type="tel"' in html
    assert 'name="cpf_cnpj"' in html and 'inputmode="numeric"' in html


# --- arquivos (foto e CNH) ----------------------------------------------------


def _pdf(nome="cnh.pdf"):
    return SimpleUploadedFile(nome, b"%PDF-1.4 teste", content_type="application/pdf")


def test_trocar_cnh_marcando_remover_e_arquivo_novo_nao_da_erro(usuario_logado, cliente):
    cliente.cnh_frente = _pdf("antiga.pdf")
    cliente.save()
    resposta = usuario_logado.post(
        f"/clientes/{cliente.pk}/editar/",
        {
            "nome": cliente.nome,
            "cpf_cnpj": cliente.cpf_cnpj,
            "status": "ativo",
            "cnh_frente": _pdf("nova.pdf"),
            "cnh_frente-clear": "on",
        },
    )
    assert resposta.status_code == 302
    cliente.refresh_from_db()
    assert "nova" in cliente.cnh_frente.name


def test_campo_de_arquivo_nao_mostra_caminho_tecnico(usuario_logado, cliente):
    cliente.cnh_frente = _pdf("antiga.pdf")
    cliente.save()
    html = usuario_logado.get(f"/clientes/{cliente.pk}/editar/").content.decode()
    assert "Ver arquivo atual" in html
    assert "Atualmente" not in html
    assert "Remover" in html


def test_extensao_errada_tem_mensagem_simples(db):
    arquivo = SimpleUploadedFile("cnh.exe", b"x", content_type="application/octet-stream")
    form = ClienteForm(data=_dados(), files={"cnh_frente": arquivo})
    assert not form.is_valid()
    assert form.errors["cnh_frente"] == ["Envie foto (JPG, PNG ou WebP) ou PDF do documento."]


def test_erro_no_formulario_avisa_que_a_cnh_nao_foi_salva(usuario_logado):
    resposta = usuario_logado.post(
        "/clientes/novo/", {**_dados(cpf_cnpj="123"), "cnh_verso": _pdf("verso.pdf")}
    )
    html = resposta.content.decode()
    assert "O arquivo “verso.pdf” não foi salvo" in html
