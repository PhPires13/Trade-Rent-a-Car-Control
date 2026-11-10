"""Etapa 18c (multas): sobras da etapa 18 e refatorações sem mudar o que a tela mostra.

Órgão repetido com mensagem clara, aviso do "Já indiquei o condutor" em
linguagem simples, __str__ que não quebra com o carro apagado, R$ no /admin
e a conferência do AIT repetido feita no banco.
"""

import random
import string
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.messages import get_messages
from django.forms.utils import ErrorDict
from django.urls import reverse
from django.utils import timezone

from apps.frota.models import Veiculo
from apps.multas.forms import MultaForm, OrgaoForm, normalizar_ait
from apps.multas.models import Multa, OrgaoAutuador

HOJE = timezone.localdate()


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
