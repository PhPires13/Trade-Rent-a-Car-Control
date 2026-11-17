"""Etapa 18c (multas): sobras da etapa 18 e refatorações sem mudar o que a tela mostra.

Órgão repetido com mensagem clara, aviso do "Já indiquei o condutor" em
linguagem simples, __str__ que não quebra com o carro apagado, R$ no /admin
e a conferência do AIT repetido feita no banco.
"""

import random
import string
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from django.contrib.messages import get_messages
from django.forms.utils import ErrorDict
from django.urls import reverse
from django.utils import timezone

from apps.frota.models import Veiculo
from apps.multas.forms import MultaForm, OrgaoForm, normalizar_ait
from apps.multas.models import Multa, OrgaoAutuador

HOJE = timezone.localdate()
NUM_CONSULTAS_NIC = 8  # registrar_nic de multa sem cliente: eram 15


@pytest.fixture
def carro(db):
    return Veiculo.objects.create(placa="SHN2E34", marca_modelo="Argo")


def _multa(veiculo, **campos):
    campos.setdefault("data_infracao", HOJE - timedelta(days=20))
    campos.setdefault("valor", Decimal("195.23"))
    campos.setdefault("descricao", "Avançar sinal")
    return Multa.objects.create(veiculo=veiculo, **campos)


def _dados(veiculo, **extra):
    dados = {
        "veiculo": veiculo.pk,
        "data_infracao": (HOJE - timedelta(days=20)).isoformat(),
        "descricao": "Avançar sinal",
        "valor": "195,23",
        "tipo_condutor": Multa.TipoCondutor.CLIENTE,
        "responsavel": Multa.Responsavel.CLIENTE,
    }
    dados.update(extra)
    return dados


# --- (1) órgão repetido ---


def test_orgao_repetido_explica_o_que_fazer(db):
    OrgaoAutuador.objects.create(nome="DER-MG")
    form = OrgaoForm({"nome": "DER-MG"})
    assert not form.is_valid()
    erro = str(form.errors["nome"])
    assert "Já existe um órgão com este nome" in erro
    assert "com este Nome já existe" not in erro  # a padrão do Django não diz o que fazer


# --- (2) aviso do "Já indiquei o condutor" ---


def test_aviso_de_fici_indicado_em_linguagem_simples(usuario_logado, carro):
    multa = _multa(carro, fici_status=Multa.Fici.PENDENTE, fici_prazo=HOJE + timedelta(days=5))
    resposta = usuario_logado.post(reverse("multas:indicar_fici", args=[multa.pk]))
    assert resposta.status_code == 302
    texto = [str(m) for m in get_messages(resposta.wsgi_request)]
    assert texto == [
        "Pronto: anotado que você já informou ao órgão quem dirigia o carro SHN2E34 "
        f"na multa de {multa.data_infracao:%d/%m/%Y} (data da indicação: hoje)."
    ]
    assert "FICI indicado para" not in texto[0]
    multa.refresh_from_db()
    assert multa.fici_status == Multa.Fici.INDICADO
    assert multa.fici_data_indicacao == HOJE


# --- (3) __str__ com o carro apagado ---


def test_str_da_multa_sem_carro_nao_quebra(db):
    multa = Multa(veiculo_id=999_999, data_infracao=HOJE, descricao="Avançar sinal")
    assert str(multa) == f"carro apagado — Avançar sinal em {HOJE:%d/%m/%Y}"


def test_str_da_multa_normal_continua_igual(carro):
    multa = _multa(carro, data_infracao=HOJE)
    assert str(multa) == f"SHN2E34 — Avançar sinal em {HOJE:%d/%m/%Y}"


def test_registro_do_historico_com_carro_apagado_nao_quebra(carro):
    multa = _multa(carro)
    registro = multa.history.first()
    registro.veiculo_id = 999_999  # o carro do registro antigo não existe mais
    assert "carro apagado" in str(registro.instance)


# --- (4) R$ no /admin ---


def test_admin_de_multas_mostra_valor_em_reais(admin_client, carro):
    _multa(carro, valor=Decimal("1234.50"))
    resposta = admin_client.get(reverse("admin:multas_multa_changelist"))
    assert resposta.status_code == 200
    assert "R$ 1.234,50" in resposta.content.decode()


# --- (5) AIT repetido conferido no banco: mesmo resultado de antes ---


def _conferir(ait, orgao, veiculo):
    """Roda só a conferência do AIT (como o clean() faz) e devolve o erro, se houver."""
    form = MultaForm()
    form._errors, form.cleaned_data = ErrorDict(), {}
    form._conferir_ait_repetido({"ait": ait, "orgao": orgao, "veiculo": veiculo})
    return form.errors.get("ait")


def _repetida_como_antes(ait, orgao, veiculo, excluir=None):
    """A conferência antiga (percorria todas em Python) — referência do resultado."""
    for multa in Multa.objects.exclude(ait="").exclude(pk=excluir).order_by("pk"):
        if normalizar_ait(multa.ait) == ait and (
            orgao is None
            or multa.orgao_id is None
            or multa.orgao_id == orgao.pk
            or (veiculo is not None and multa.veiculo_id == veiculo.pk)
        ):
            return multa
    return None


@pytest.mark.parametrize(
    "gravado", ["der123", "DER-123", "der 123", "DÉR.123", "DÉR123", "ＤＥＲ１２３", "DER123"]
)
def test_ait_gravado_fora_do_padrao_ainda_e_achado(carro, gravado):
    """AIT gravado sem normalizar (/admin, dado antigo) continua barrando a repetição."""
    orgao = OrgaoAutuador.objects.create(nome="DER")
    original = _multa(carro, orgao=orgao)
    Multa.objects.filter(pk=original.pk).update(ait=gravado)
    form = MultaForm(_dados(carro, ait="der-123", orgao=orgao.pk))
    assert not form.is_valid()
    assert "já foi cadastrada" in str(form.errors["ait"])


def test_ait_parecido_mas_diferente_passa(carro):
    orgao = OrgaoAutuador.objects.create(nome="DER")
    original = _multa(carro, orgao=orgao)
    Multa.objects.filter(pk=original.pk).update(ait="DER-1234")
    assert MultaForm(_dados(carro, ait="DER123", orgao=orgao.pk)).is_valid()


def test_ait_em_outro_orgao_mas_mesmo_carro_e_barrado(carro):
    _multa(carro, ait="ABC999", orgao=OrgaoAutuador.objects.create(nome="DER"))
    prf = OrgaoAutuador.objects.create(nome="PRF")
    form = MultaForm(_dados(carro, ait="abc 999", orgao=prf.pk))
    assert "ait" in form.errors


def test_editar_a_propria_multa_nao_acusa_repeticao(carro):
    orgao = OrgaoAutuador.objects.create(nome="DER")
    multa = _multa(carro, ait="ABC999", orgao=orgao)
    dados = _dados(carro, ait="ABC-999", orgao=orgao.pk)
    form = MultaForm(dados, instance=multa)
    assert form.is_valid(), form.errors


def test_ait_repetido_da_o_mesmo_resultado_da_conferencia_antiga(db):
    """Base sorteada com AITs sujos, órgãos e carros: o banco escolhe a mesma multa."""
    sorteio = random.Random(18)
    carros = [Veiculo.objects.create(placa=f"AAA0A{i:02d}", marca_modelo="Gol") for i in range(3)]
    orgaos = [OrgaoAutuador.objects.create(nome=nome) for nome in ("DER", "PRF", "BHTrans")]
    sujeira = ["", "-", " ", ".", "/"]
    for _ in range(120):
        base = sorteio.choice(["AB12", "ab12", "XY9", "Z1", "É7", "E7"])
        bruto = "".join(c + sorteio.choice(sujeira) for c in base)
        Multa.objects.create(
            veiculo=sorteio.choice(carros),
            orgao=sorteio.choice([*orgaos, None]),
            data_infracao=HOJE,
            descricao="x",
            ait=sorteio.choice([bruto, bruto.upper(), ""]),
        )
    Multa.objects.create(veiculo=carros[0], data_infracao=HOJE, ait="")
    for ait in ["AB12", "XY9", "Z1", "E7", "NADA1"]:
        for orgao in [*orgaos, None]:
            for veiculo in [*carros, None]:
                erro = _conferir(ait, orgao, veiculo)
                antes = _repetida_como_antes(ait, orgao, veiculo)
                if antes is None:
                    assert erro is None, (ait, orgao, veiculo)
                else:
                    assert f"AIT {antes.ait}, {antes.veiculo.placa}" in str(erro)


def test_conferencia_do_ait_e_uma_consulta_so(carro, django_assert_num_queries):
    orgao = OrgaoAutuador.objects.create(nome="DER")
    for numero in range(30):
        _multa(carro, ait=f"N{numero}-{string.ascii_uppercase[numero % 26]}", orgao=orgao)
    with django_assert_num_queries(1):
        erro = _conferir("N5F", orgao, carro)
    assert "já foi cadastrada" in str(erro)


# --- refatoração: Gerar ND de multas com consultas fixas ---


def _cliente_com_carro(placa="QXQ6C10"):
    from apps.alocacoes.models import Alocacao
    from apps.pessoas.models import Cliente

    cliente = Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")
    veiculo = Veiculo.objects.create(placa=placa, marca_modelo="Gol")
    Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=400),
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
    )
    return cliente, veiculo


def _multas_a_cobrar(veiculo, quantidade, inicio):
    return [
        _multa(
            veiculo,
            data_infracao=inicio + timedelta(days=i),
            ait=f"AIT{i}" if i % 2 else "",
            codigo="7455-0" if i % 3 == 0 else "",
            descricao=f"Infração {i}",
            valor=Decimal("100.00") + i,
        )
        for i in range(quantidade)
    ]


def _consultas_da_nd(cliente, multas, django_assert_num_queries, quantas):
    from apps.multas import services

    with django_assert_num_queries(quantas):
        return services.gerar_nd_de_multas(cliente, multas, HOJE)


def test_gerar_nd_de_multas_tem_consultas_fixas_e_o_mesmo_resultado(db, django_assert_num_queries):
    """10 ou 40 multas: o mesmo número de consultas, e itens/cobrança como antes."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.multas import services

    cliente, veiculo = _cliente_com_carro()
    poucas = _multas_a_cobrar(veiculo, 10, HOJE - timedelta(days=300))
    with CaptureQueriesContext(connection) as consultas:
        primeira = services.gerar_nd_de_multas(cliente, poucas, HOJE)
    muitas = _multas_a_cobrar(veiculo, 40, HOJE - timedelta(days=200))
    segunda = _consultas_da_nd(cliente, muitas, django_assert_num_queries, len(consultas))

    assert (primeira.numero, segunda.numero) == (1, 2)
    itens = list(segunda.itens.order_by("pk").values_list("descricao", "valor", "multa_id"))
    assert [multa_id for _, _, multa_id in itens] == [m.pk for m in muitas]
    assert itens[0] == (
        f"Multa — QXQ6C10 — Infração 0 ({muitas[0].data_infracao:%d/%m/%Y}) · cód. 7455-0",
        Decimal("100.00"),
        muitas[0].pk,
    )
    assert itens[1] == (
        f"Multa AIT AIT1 — QXQ6C10 — Infração 1 ({muitas[1].data_infracao:%d/%m/%Y})",
        Decimal("101.00"),
        muitas[1].pk,
    )
    cobranca = segunda.cobranca
    assert cobranca.descricao == "Nota de débito ND 002 (multas)"
    assert cobranca.valor == sum(m.valor for m in muitas) == segunda.total
    assert cobranca.status == "pendente"
    for multa in muitas:
        multa.refresh_from_db()
        assert multa.repasse == "Incluída na ND 002"


def test_gerar_nd_de_novo_com_multa_ja_em_nd_continua_recusado(db):
    from django.core.exceptions import ValidationError

    from apps.multas import services

    cliente, veiculo = _cliente_com_carro()
    multas = _multas_a_cobrar(veiculo, 3, HOJE - timedelta(days=60))
    services.gerar_nd_de_multas(cliente, multas, HOJE)
    with pytest.raises(ValidationError, match="não está 'a cobrar'"):
        services.gerar_nd_de_multas(cliente, multas, HOJE)


# --- NIC de multa sem cliente continua sem cliente (e sem consultas à toa) ---


@pytest.fixture
def carro_com_bia_hoje(db):
    """Carro sem ninguém na data da infração; hoje está com Bia."""
    from apps.alocacoes.models import Alocacao
    from apps.pessoas.models import Cliente

    bia = Cliente.objects.create(nome="Bia", cpf_cnpj="222.333.444-55")
    veiculo = Veiculo.objects.create(placa="NIC1A23", marca_modelo="Onix")
    Alocacao.objects.create(
        veiculo=veiculo,
        cliente=bia,
        data_inicio=HOJE - timedelta(days=10),
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
    )
    return veiculo, bia


def _dados_edicao(multa, **extra):
    dados = {
        "veiculo": multa.veiculo_id,
        "data_infracao": multa.data_infracao.isoformat(),
        "cliente": multa.cliente_id or "",
        "descricao": multa.descricao,
        "valor": str(multa.valor).replace(".", ",") if multa.valor is not None else "",
        "ait": multa.ait,
        "orgao": multa.orgao_id or "",
        "fici_prazo": multa.fici_prazo.isoformat() if multa.fici_prazo else "",
        "tipo_condutor": multa.tipo_condutor,
        "responsavel": multa.responsavel,
        "resultado": multa.resultado,
        "fici_status": multa.fici_status,
        "pagamento": multa.pagamento,
        "pago_por": multa.pago_por,
    }
    dados.update(extra)
    return dados


def test_nic_de_multa_sem_cliente_fica_sem_cliente_no_banco_e_no_historico(carro_com_bia_hoje):
    from apps.multas import services

    veiculo, _ = carro_com_bia_hoje
    original = _multa(veiculo, data_infracao=HOJE - timedelta(days=60))
    assert original.cliente_id is None
    nic = services.registrar_nic(original, valor=Decimal("195.30"))
    nic.refresh_from_db()
    original.refresh_from_db()
    assert nic.cliente_id is None
    assert [registro.cliente_id for registro in nic.history.all()] == [None]
    assert original.cliente_id is None
    assert original.fici_status == Multa.Fici.PRAZO_PERDIDO


def test_editar_a_nic_sem_cliente_nao_poe_o_motorista_de_hoje(usuario_logado, carro_com_bia_hoje):
    from apps.multas import services

    veiculo, _ = carro_com_bia_hoje
    original = _multa(veiculo, data_infracao=HOJE - timedelta(days=60))
    nic = services.registrar_nic(original, valor=Decimal("195.30"))
    nic.refresh_from_db()
    resposta = usuario_logado.post(
        reverse("multas:editar", args=[nic.pk]), _dados_edicao(nic, observacoes="anotação")
    )
    assert resposta.status_code == 302
    nic.refresh_from_db()
    assert nic.observacoes == "anotação"
    assert nic.cliente_id is None
    assert {registro.cliente_id for registro in nic.history.all()} == {None}


def test_nic_de_multa_com_cliente_leva_o_cliente_da_original(carro_com_bia_hoje):
    from apps.multas import services

    veiculo, bia = carro_com_bia_hoje
    original = _multa(veiculo, data_infracao=HOJE - timedelta(days=2))
    assert original.cliente == bia
    nic = services.registrar_nic(original, valor=Decimal("195.30"))
    nic.refresh_from_db()
    assert nic.cliente == bia


def test_multa_comum_sem_cliente_continua_ganhando_quem_estava_com_o_carro(
    usuario_logado, carro_com_bia_hoje
):
    veiculo, bia = carro_com_bia_hoje
    resposta = usuario_logado.post(
        reverse("multas:nova"),
        _dados(veiculo, data_infracao=(HOJE - timedelta(days=2)).isoformat()),
    )
    assert resposta.status_code == 302
    assert Multa.objects.get().cliente == bia


def test_salvar_so_um_campo_nao_procura_o_cliente(carro_com_bia_hoje, django_assert_num_queries):
    """save(update_fields=[...]) sem 'cliente' não grava o cliente: não precisa buscá-lo."""
    veiculo, _ = carro_com_bia_hoje
    multa = _multa(veiculo, data_infracao=HOJE - timedelta(days=60))
    multa.fici_status = Multa.Fici.INDICADO
    with django_assert_num_queries(2):  # UPDATE + registro do histórico
        multa.save(update_fields=["fici_status"])
    multa.refresh_from_db()
    assert multa.cliente_id is None
    assert multa.fici_status == Multa.Fici.INDICADO


def test_registrar_nic_sem_cliente_gasta_menos_consultas(
    carro_com_bia_hoje, django_assert_num_queries
):
    from apps.multas import services

    veiculo, _ = carro_com_bia_hoje
    original = _multa(veiculo, data_infracao=HOJE - timedelta(days=60))
    with django_assert_num_queries(NUM_CONSULTAS_NIC):
        services.registrar_nic(original, valor=Decimal("195.30"))


# --- valor negativo barrado também pelo /admin e no banco (Etapa 15: sem negativos) ---


def _form_do_admin(modelo, admin_user, rf):
    from django.contrib import admin

    pedido = rf.get("/")
    pedido.user = admin_user
    return admin.site._registry[modelo].get_form(pedido)


def test_admin_recusa_multa_com_valor_negativo(carro, admin_user, rf):
    form_admin = _form_do_admin(Multa, admin_user, rf)
    dados = {
        "veiculo": carro.pk,
        "data_infracao": HOJE.isoformat(),
        "valor": "-130.16",
        "resultado": Multa.Resultado.EM_ABERTO,
        "tipo_condutor": Multa.TipoCondutor.CLIENTE,
        "fici_status": Multa.Fici.PENDENTE,
        "pagamento": Multa.Pagamento.PENDENTE,
        "responsavel": Multa.Responsavel.CLIENTE,
    }
    form = form_admin(dados)
    assert not form.is_valid()
    assert form.errors["valor"] == ["O valor não pode ser negativo."]
    dados["valor"] = "130.16"
    assert form_admin(dados).is_valid(), form_admin(dados).errors


def test_banco_recusa_multa_com_valor_negativo(carro):
    from django.db import IntegrityError, transaction

    multa = _multa(carro)
    with pytest.raises(IntegrityError), transaction.atomic():
        Multa.objects.filter(pk=multa.pk).update(valor=Decimal("-1"))
    Multa.objects.filter(pk=multa.pk).update(valor=None)  # vazio (advertência) continua valendo
    Multa.objects.filter(pk=multa.pk).update(valor=Decimal("0"))  # zero = "não se aplica"


# --- revisão: a trava nova do modelo não repete o aviso na tela da multa ---


def test_tela_da_multa_avisa_valor_negativo_uma_vez_so(carro):
    form = MultaForm(_dados(carro, valor="-130,16"))
    assert not form.is_valid()
    assert form.errors["valor"] == ["O valor não pode ser negativo."]
    assert not form.non_field_errors()


def test_valor_zero_e_vazio_continuam_aceitos_na_tela(carro):
    assert MultaForm(_dados(carro, valor="0,00")).is_valid()
    assert MultaForm(_dados(carro, valor="")).is_valid()


# --- regressão no navegador: Voltar + "Registrar" de novo (multa sem AIT) ---


def _registrar(client, veiculo, **extra):
    dados = _dados(veiculo, ait="", token=uuid4().hex, **extra)
    return client.post(reverse("multas:nova"), dados)


def test_voltar_e_registrar_de_novo_sem_ait_nao_grava_em_dobro(usuario_logado, carro):
    """Voltar busca a página com um código novo: o código sozinho não segurava."""
    _registrar(usuario_logado, carro)
    resposta = _registrar(usuario_logado, carro)
    assert Multa.objects.count() == 1
    html = resposta.content.decode()
    assert "acabou de ser registrada" in html
    assert reverse("multas:editar", args=[Multa.objects.get().pk]) in html


def test_mesma_multa_sem_valor_tambem_nao_grava_em_dobro(usuario_logado, carro):
    _registrar(usuario_logado, carro, valor="")
    _registrar(usuario_logado, carro, valor="")
    assert Multa.objects.count() == 1


def test_multa_sem_ait_diferente_continua_sendo_registrada(usuario_logado, carro):
    _registrar(usuario_logado, carro)
    _registrar(usuario_logado, carro, descricao="Estacionar em local proibido")
    _registrar(usuario_logado, carro, valor="130,16")
    assert Multa.objects.count() == 3


def test_mesma_multa_sem_ait_registrada_ha_tempo_nao_e_barrada(usuario_logado, carro):
    """A janela é só contra reenvio: o aviso de multa repetida sem AIT é dos donos."""
    _registrar(usuario_logado, carro)
    Multa.history.update(history_date=timezone.now() - timedelta(minutes=11))
    _registrar(usuario_logado, carro)
    assert Multa.objects.count() == 2


def test_reenvio_de_multa_apagada_nao_e_barrado(usuario_logado, carro):
    _registrar(usuario_logado, carro)
    Multa.objects.all().delete()
    _registrar(usuario_logado, carro)
    assert Multa.objects.count() == 1


def test_editar_a_multa_recem_registrada_nao_acusa_reenvio(carro):
    from apps.multas.forms import MultaEdicaoForm

    multa = MultaForm(_dados(carro)).save()
    form = MultaEdicaoForm(_dados_edicao(multa, observacoes="ok"), instance=multa)
    assert form.is_valid(), form.errors


def test_nova_multa_nao_deixa_o_navegador_devolver_os_campos(usuario_logado, carro):
    html = usuario_logado.get(reverse("multas:nova")).content.decode()
    assert 'autocomplete="off"' in html
    assert "data-aviso-voltar" in html and "back_forward" in html
    multa = _multa(carro)
    html = usuario_logado.get(reverse("multas:editar", args=[multa.pk])).content.decode()
    assert "data-aviso-voltar" not in html


# --- AIT repetido: texto certo na edição e na nova, com link para a outra multa ---


def test_ait_repetido_na_edicao_nao_fala_em_cadastrar(usuario_logado, carro):
    outra = _multa(carro, ait="SEEDE2E002", data_infracao=HOJE - timedelta(days=5))
    multa = _multa(carro, ait="OUTRO1")
    resposta = usuario_logado.post(
        reverse("multas:editar", args=[multa.pk]), _dados_edicao(multa, ait="SEEDE2E002")
    )
    html = resposta.content.decode()
    assert "Outra multa já usa este AIT" in html
    assert f"{outra.data_infracao:%d/%m/%Y}" in html
    assert reverse("multas:editar", args=[outra.pk]) in html
    assert "cadastrar de novo" not in html and "Concluídas" not in html
    multa.refresh_from_db()
    assert multa.ait == "OUTRO1"


def test_ait_repetido_na_nova_aponta_a_multa_e_nao_o_filtro(usuario_logado, carro):
    outra = _multa(carro, ait="SEEDE2E002")
    resposta = usuario_logado.post(
        reverse("multas:nova"), _dados(carro, ait="SEEDE2E002", token=uuid4().hex)
    )
    html = resposta.content.decode()
    assert "já foi cadastrada" in html
    assert reverse("multas:editar", args=[outra.pk]) in html
    assert "Concluídas" not in html
    assert Multa.objects.count() == 1


# --- campo travado pela ND: texto neutro e caminho que existe ---


@pytest.mark.parametrize(
    ("campo", "valor"),
    [("valor", "300,00"), ("responsavel", Multa.Responsavel.EMPRESA), ("cliente", "")],
)
def test_campo_travado_pela_nd_explica_o_caminho(db, campo, valor):
    from apps.multas import services
    from apps.multas.forms import MultaEdicaoForm

    cliente, veiculo = _cliente_com_carro()
    multa = _multa(veiculo, cliente=cliente, data_infracao=HOJE - timedelta(days=30))
    services.gerar_nd_de_multas(cliente, [multa], HOJE)
    multa.refresh_from_db()
    form = MultaEdicaoForm(_dados_edicao(multa, **{campo: valor}), instance=multa)
    assert not form.is_valid()
    erro = str(form.errors[campo])
    assert "ND 001, que está sendo cobrada do cliente" in erro
    assert "cancele a ND em Financeiro &gt; Notas de débito" in erro or (
        "cancele a ND em Financeiro > Notas de débito" in erro
    )
    assert "valor antigo" not in erro and "Ajuste" not in erro


# --- Gerar ND com tudo desmarcado: sem perguntar "emitir R$ 0,00?" ---


def test_gerar_nd_sem_multa_marcada_trava_o_botao(usuario_logado):
    cliente, veiculo = _cliente_com_carro()
    _multa(veiculo, cliente=cliente, data_infracao=HOJE - timedelta(days=30))
    html = usuario_logado.get(reverse("multas:gerar_nd"), {"cliente": cliente.pk}).content.decode()
    assert "marcadas: 1" in html
    assert "if (!marcadas) { $event.preventDefault(); return }" in html
    assert ':disabled="!marcadas"' in html
    assert "Marque ao menos uma multa." in html
