"""Busca global sem acento, por CPF/telefone/condutor/AIT/BO (proposta #18) e
lista de clientes no celular (proposta #13)."""

from datetime import date

import pytest

from apps.busca import parece_numero, sem_acento, so_letras_e_digitos
from apps.frota.models import Veiculo
from apps.multas.models import Multa
from apps.pessoas.models import Cliente, CondutorAutorizado
from apps.sinistros.models import Sinistro


@pytest.fixture
def joao(db):
    return Cliente.objects.create(
        nome="João da Conceição", cpf_cnpj="123.456.789-01", telefone="(11) 98888-7777"
    )


@pytest.fixture
def carro(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Fiat Argo")


def test_normalizacao_de_texto():
    assert sem_acento("  JOÃO  da Conceição ") == "joao da conceicao"
    assert so_letras_e_digitos("r-123.456 / 2025") == "R1234562025"
    assert parece_numero("123.456.789-01")
    assert parece_numero("+55 (11) 98888-7777")
    assert not parece_numero("ABC1D23")  # placa tem letra: não é CPF/telefone
    assert not parece_numero("12")  # curto demais


@pytest.mark.parametrize("digitado", ["joao", "CONCEICAO", "joão da conceição", "Conceicao"])
def test_busca_ignora_acento_e_maiuscula(usuario_logado, joao, digitado):
    resposta = usuario_logado.get("/buscar/", {"q": digitado})
    assert resposta.status_code == 302
    assert resposta.url == f"/clientes/{joao.pk}/"


@pytest.mark.parametrize(
    "digitado", ["12345678901", "123.456.789-01", "11988887777", "+55 11 98888-7777", "8888-7777"]
)
def test_busca_por_cpf_ou_telefone_com_ou_sem_pontuacao(usuario_logado, joao, digitado):
    resposta = usuario_logado.get("/buscar/", {"q": digitado})
    assert resposta.status_code == 302
    assert resposta.url == f"/clientes/{joao.pk}/"


def test_busca_pelo_condutor_leva_ao_cliente(usuario_logado, joao):
    CondutorAutorizado.objects.create(nome="Zé Ramos", cliente=joao)
    resposta = usuario_logado.get("/buscar/", {"q": "ze ramos"})
    assert resposta.status_code == 302
    assert resposta.url == f"/clientes/{joao.pk}/"


def test_condutor_sem_cliente_aparece_no_proprio_grupo(usuario_logado, db):
    condutor = CondutorAutorizado.objects.create(nome="Ângela Souza")
    resposta = usuario_logado.get("/buscar/", {"q": "angela"})
    assert resposta.url == f"/clientes/condutores/{condutor.pk}/editar/"


def test_busca_por_ait_e_por_bo(usuario_logado, carro):
    multa = Multa.objects.create(veiculo=carro, data_infracao=date(2026, 7, 10), ait="R-123.456")
    sinistro = Sinistro.objects.create(
        veiculo=carro,
        data=date(2026, 7, 10),
        envolvido="terceiro",
        boletim_ocorrencia="BO 9876/2026",
    )
    assert usuario_logado.get("/buscar/", {"q": "r123456"}).url == f"/multas/{multa.pk}/editar/"
    assert usuario_logado.get("/buscar/", {"q": "9876"}).url == f"/sinistros/{sinistro.pk}/editar/"


def test_resultados_agrupados_quando_ha_varios(usuario_logado, joao, carro):
    CondutorAutorizado.objects.create(nome="Joana Lima", cliente=None)
    Cliente.objects.create(nome="Joaquim Argolo", cpf_cnpj="999.888.777-66")
    resposta = usuario_logado.get("/buscar/", {"q": "jo"})
    assert resposta.status_code == 200
    html = resposta.content.decode()
    assert "Clientes" in html and "Condutores sem cliente ligado" in html
    assert resposta.context["total"] == 3
    # o modelo do carro também é achado sem acento e em meio a outros grupos
    resposta = usuario_logado.get("/buscar/", {"q": "argo"})
    assert [v.pk for v in resposta.context["veiculos"]] == [carro.pk]
    assert len(resposta.context["clientes"]) == 1


def test_motivo_explica_por_que_o_cliente_apareceu(usuario_logado, joao):
    Cliente.objects.create(nome="Outro", cpf_cnpj="000.000.000-00", telefone="(11) 98888-7777")
    resposta = usuario_logado.get("/buscar/", {"q": "98888-7777"})
    assert resposta.status_code == 200
    assert "achado pelo telefone" in resposta.content.decode()


def test_nada_encontrado_explica_o_que_da_para_buscar(usuario_logado, db):
    html = usuario_logado.get("/buscar/", {"q": "zzzz"}).content.decode()
    assert "Nenhum resultado" in html and "AIT" in html
    assert "placa, nome, CPF, telefone ou nº da multa" in html


def test_lista_de_clientes_busca_sem_acento_e_por_cpf(usuario_logado, joao):
    Cliente.objects.create(nome="Maria", cpf_cnpj="222.333.444-55")
    resposta = usuario_logado.get("/clientes/", {"q": "conceicao"})
    assert [c["cliente"].pk for c in resposta.context["cards"]] == [joao.pk]
    resposta = usuario_logado.get("/clientes/", {"q": "12345678901"})
    assert [c["cliente"].pk for c in resposta.context["cards"]] == [joao.pk]


def test_lista_de_clientes_tem_botoes_grandes_no_celular(usuario_logado, joao):
    html = usuario_logado.get("/clientes/").content.decode()
    assert "Nome, CPF ou telefone" in html
    assert html.count("min-h-11") >= 3  # novo cliente, buscar e WhatsApp


# --- reforços da revisão ---------------------------------------------------


def test_busca_exige_login(client, db):
    resposta = client.get("/buscar/", {"q": "joao"})
    assert resposta.status_code == 302 and resposta.url.startswith("/entrar/")


def test_cnpj_que_comeca_com_55_nao_perde_os_dois_primeiros_numeros(usuario_logado, db):
    """O "+55" do WhatsApp só é tirado de telefone (12/13 dígitos), nunca de CNPJ."""
    Cliente.objects.create(nome="Empresa A", cpf_cnpj="55.123.456/0001-00")
    outro = Cliente.objects.create(nome="Empresa B", cpf_cnpj="00.123.456/0001-00")
    resposta = usuario_logado.get("/buscar/", {"q": "55.123.456/0001-00"})
    assert resposta.status_code == 302
    assert resposta.url != f"/clientes/{outro.pk}/"


def test_multa_sem_ait_mostra_o_numero_do_processo(usuario_logado, carro):
    Multa.objects.create(veiculo=carro, data_infracao=date(2026, 7, 10), num_processamento="P-5555")
    Multa.objects.create(veiculo=carro, data_infracao=date(2026, 7, 11), ait="X5555")
    html = usuario_logado.get("/buscar/", {"q": "5555"}).content.decode()
    assert "Processo P-5555" in html and "AIT X5555" in html
    assert "AIT P-5555" not in html


def test_nome_curto_sem_numero_nao_procura_multa(usuario_logado, carro, joao):
    Multa.objects.create(veiculo=carro, data_infracao=date(2026, 7, 10), ait="JOAO")
    resposta = usuario_logado.get("/buscar/", {"q": "joao"})
    assert resposta.url == f"/clientes/{joao.pk}/"


def test_condutor_aparece_como_motivo_na_lista_agrupada(usuario_logado, joao):
    CondutorAutorizado.objects.create(nome="Zé Ramos", cliente=joao)
    Cliente.objects.create(nome="Ramos Filho")
    html = usuario_logado.get("/buscar/", {"q": "RAMOS"}).content.decode()
    assert "achado pelo condutor autorizado: Zé Ramos" in html


@pytest.mark.parametrize("digitado", ["%", "_", "-", "'; drop", "ç" * 300])
def test_consultas_estranhas_nao_dao_erro(usuario_logado, joao, carro, digitado):
    assert usuario_logado.get("/buscar/", {"q": digitado}).status_code in (200, 302)
    assert usuario_logado.get("/clientes/", {"q": digitado, "status": "xyz"}).status_code == 200


def test_lista_avisa_que_inativo_nao_aparece(usuario_logado, db):
    Cliente.objects.create(nome="Ex Cliente", status=Cliente.Status.INATIVO)
    html = usuario_logado.get("/clientes/", {"q": "ex cliente"}).content.decode()
    assert "Clientes inativos não aparecem aqui" in html
    assert "/buscar/?q=ex%20cliente" in html
    # a busca do topo acha o inativo
    resposta = usuario_logado.get("/buscar/", {"q": "ex cliente"})
    assert resposta.status_code == 302
