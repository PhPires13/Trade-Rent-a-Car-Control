"""Etapa 18c — refatoração da área de manutenção.

Registrar/editar: o efeito no carro (e o repasse) fica na mesma transação da
manutenção — falhou, nada fica gravado pela metade. Travas no banco para datas
da oficina e valores negativos que o Admin deixava passar.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib import admin
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.frota.models import Veiculo
from apps.manutencao import views
from apps.manutencao.models import Manutencao

HOJE = timezone.localdate()


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="ETA1C18", marca_modelo="Onix", km_atual=100_000)


def _dados(**extra):
    dados = {
        "tipo": "corretiva",
        "data": HOJE.isoformat(),
        "km": "100000",
        "descricao": "Troca da embreagem",
        "origem_custo": "particular",
        "responsavel": "empresa",
        "pagamento_custo": "pendente",
        "data_entrada": HOJE.isoformat(),
    }
    dados.update(extra)
    return dados


def _mensagens(resposta):
    return [str(m) for m in resposta.context["messages"]]


# --- registrar/editar numa transação só ----------------------------------------------


def test_registrar_mensagens_na_mesma_ordem(usuario_logado, veiculo):
    resposta = usuario_logado.post(
        f"/manutencao/registrar/{veiculo.pk}/", _dados(token="t-ordem"), follow=True
    )
    mensagens = _mensagens(resposta)
    assert mensagens[0] == f"Manutenção registrada para {veiculo.placa}."
    assert "ficou “Em manutenção”" in mensagens[1]
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.EM_MANUTENCAO


def test_registrar_com_falha_no_efeito_nao_grava_pela_metade(usuario_logado, veiculo, monkeypatch):
    def quebra(*_args, **_kwargs):
        raise RuntimeError("falha simulada")

    monkeypatch.setattr(views, "_avisar_cobranca", quebra)
    with pytest.raises(RuntimeError):
        usuario_logado.post(f"/manutencao/registrar/{veiculo.pk}/", _dados(token="t-falha"))
    assert not Manutencao.objects.exists()
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.DISPONIVEL

    # o código do formulário não ficou "usado": reenviar grava normalmente
    monkeypatch.undo()
    usuario_logado.post(f"/manutencao/registrar/{veiculo.pk}/", _dados(token="t-falha"))
    assert Manutencao.objects.count() == 1
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.EM_MANUTENCAO


def test_editar_com_falha_no_efeito_desfaz_a_edicao(usuario_logado, veiculo, monkeypatch):
    usuario_logado.post(f"/manutencao/registrar/{veiculo.pk}/", _dados(token="t-edit"))
    manutencao = Manutencao.objects.get()

    def quebra(*_args, **_kwargs):
        raise RuntimeError("falha simulada")

    monkeypatch.setattr(views, "_carro_voltou", quebra)
    with pytest.raises(RuntimeError):
        usuario_logado.post(
            f"/manutencao/editar/{manutencao.pk}/", _dados(data_saida=HOJE.isoformat())
        )
    manutencao.refresh_from_db()
    assert manutencao.data_saida is None
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.EM_MANUTENCAO


# --- travas no banco ------------------------------------------------------------------


def _manutencao(veiculo, **extra):
    dados = {"veiculo": veiculo, "tipo": "corretiva", "data": HOJE}
    dados.update(extra)
    return Manutencao.objects.create(**dados)


def test_banco_recusa_saida_antes_da_entrada(veiculo):
    manutencao = _manutencao(veiculo, data_entrada=HOJE)
    with pytest.raises(IntegrityError), transaction.atomic():
        Manutencao.objects.filter(pk=manutencao.pk).update(data_saida=HOJE - timedelta(days=1))


def test_banco_recusa_saida_sem_entrada(veiculo):
    with pytest.raises(IntegrityError), transaction.atomic():
        _manutencao(veiculo, data_saida=HOJE)


@pytest.mark.parametrize("campo", ["custo_real", "valor_cobrado_cliente"])
def test_banco_recusa_valor_negativo(veiculo, campo):
    with pytest.raises(IntegrityError), transaction.atomic():
        _manutencao(veiculo, **{campo: Decimal("-500.00")})


def test_banco_aceita_o_normal(veiculo):
    _manutencao(veiculo)
    _manutencao(veiculo, data_entrada=HOJE, data_saida=HOJE, custo_real=Decimal("0"))
    _manutencao(veiculo, data_entrada=HOJE - timedelta(days=3), valor_cobrado_cliente=Decimal("1"))
    assert Manutencao.objects.count() == 3


def test_admin_recusa_saida_antes_da_entrada(veiculo, rf, admin_user):
    modelo_admin = admin.site._registry[Manutencao]
    request = rf.get("/")
    request.user = admin_user
    form_class = modelo_admin.get_form(request)
    form = form_class(
        data={
            "veiculo": veiculo.pk,
            "tipo": "corretiva",
            "data": "10/10/2026",
            "data_entrada": "10/10/2026",
            "data_saida": "01/10/2026",
            "custo_real": "-500",
            "origem_custo": "particular",
            "responsavel": "empresa",
            "pagamento_custo": "pendente",
        }
    )
    assert not form.is_valid()
    texto = str(form.errors)
    assert "A saída não pode ser antes da entrada." in texto


# --- revisão: "Carro voltou" também numa transação só ------------------------------------


def test_carro_voltou_com_falha_no_efeito_nao_grava_a_saida(usuario_logado, veiculo, monkeypatch):
    usuario_logado.post(f"/manutencao/registrar/{veiculo.pk}/", _dados(token="t-voltou"))
    manutencao = Manutencao.objects.get()

    def quebra(*_args, **_kwargs):
        raise RuntimeError("falha simulada")

    monkeypatch.setattr(views, "_carro_voltou", quebra)
    with pytest.raises(RuntimeError):
        usuario_logado.post(
            f"/manutencao/voltou/{manutencao.pk}/",
            {"data_saida": HOJE.isoformat(), "pagamento_custo": "pendente"},
        )
    manutencao.refresh_from_db()
    assert manutencao.data_saida is None  # dá para tentar de novo pelo "Carro voltou"

    monkeypatch.undo()
    resposta = usuario_logado.post(
        f"/manutencao/voltou/{manutencao.pk}/",
        {"data_saida": HOJE.isoformat(), "pagamento_custo": "pendente"},
        follow=True,
    )
    manutencao.refresh_from_db()
    assert manutencao.data_saida == HOJE
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.DISPONIVEL
    mensagens = _mensagens(resposta)[-2:]  # antes: as do registro, não lidas
    assert mensagens[0] == f"{veiculo.placa} saiu da oficina — 0 dias parado."
    assert mensagens[1] == f"{veiculo.placa} voltou a ficar disponível para alugar."


# --- "Voltar" + Salvar não grava a manutenção em dobro (trava pelo conteúdo) ----------


def _registrar(cliente_http, veiculo, token, **extra):
    dados = _dados(
        token=token,
        descricao="SEED-E2E amortecedor voltar",
        custo_real="R$ 350",
        km="100.000",
        data_entrada="",
    )
    dados.update(extra)
    return cliente_http.post(f"/manutencao/registrar/{veiculo.pk}/", dados, follow=True)


def test_voltar_e_salvar_de_novo_com_token_novo_nao_duplica(usuario_logado, veiculo):
    _registrar(usuario_logado, veiculo, "t-primeiro")
    resposta = _registrar(usuario_logado, veiculo, "t-novo-depois-do-voltar")

    assert Manutencao.objects.filter(veiculo=veiculo).count() == 1
    assert "já foi registrada agora há pouco" in " ".join(_mensagens(resposta))
    assert resposta.redirect_chain[-1][0] == f"/manutencao/historico/{veiculo.pk}/"


def test_servico_diferente_logo_depois_e_gravado(usuario_logado, veiculo):
    _registrar(usuario_logado, veiculo, "t-um")
    _registrar(usuario_logado, veiculo, "t-dois", custo_real="R$ 120")
    assert Manutencao.objects.filter(veiculo=veiculo).count() == 2


def test_dois_servicos_na_mesma_ida_a_oficina_sao_gravados(usuario_logado, veiculo):
    """Mesmo dia, KM e custo em branco, descrições diferentes: não é repetição."""
    _registrar(usuario_logado, veiculo, "t-a", custo_real="", descricao="Embreagem")
    _registrar(usuario_logado, veiculo, "t-b", custo_real="", descricao="Freio")
    assert Manutencao.objects.filter(veiculo=veiculo).count() == 2


def test_servico_igual_depois_de_2_minutos_e_gravado(usuario_logado, veiculo):
    _registrar(usuario_logado, veiculo, "t-antes")
    Manutencao.history.update(history_date=timezone.now() - timedelta(minutes=3))
    _registrar(usuario_logado, veiculo, "t-depois")
    assert Manutencao.objects.filter(veiculo=veiculo).count() == 2


def test_igual_apagada_nao_trava_o_novo_registro(usuario_logado, veiculo):
    _registrar(usuario_logado, veiculo, "t-errada")
    Manutencao.objects.get().delete()
    _registrar(usuario_logado, veiculo, "t-certa")
    assert Manutencao.objects.filter(veiculo=veiculo).count() == 1


def test_formulario_novo_nao_e_restaurado_pelo_voltar(usuario_logado, veiculo):
    html = usuario_logado.get(f"/manutencao/registrar/{veiculo.pk}/").content.decode()
    assert 'autocomplete="off"' in html.split('class="form-cadastro', 1)[1].split(">", 1)[0]


# --- "Apagar" com a sessão expirada: o GET do login não vira página em branco ---------


def test_apagar_por_get_nao_apaga_e_volta_para_a_manutencao(usuario_logado, veiculo):
    manutencao = _manutencao(veiculo)
    resposta = usuario_logado.get(f"/manutencao/apagar/{manutencao.pk}/", follow=True)

    assert Manutencao.objects.filter(pk=manutencao.pk).exists()
    assert resposta.status_code == 200
    assert resposta.redirect_chain[0][0] == f"/manutencao/editar/{manutencao.pk}/"
    assert "Nada foi apagado" in " ".join(_mensagens(resposta))


def test_apagar_por_get_de_manutencao_que_nao_existe_vai_para_preventivas(usuario_logado, db):
    resposta = usuario_logado.get("/manutencao/apagar/999999/")
    assert resposta.status_code == 302
    assert resposta.url == "/manutencao/preventivas/"


def test_apagar_por_post_continua_apagando(usuario_logado, veiculo):
    manutencao = _manutencao(veiculo)
    usuario_logado.post(f"/manutencao/apagar/{manutencao.pk}/")
    assert not Manutencao.objects.filter(pk=manutencao.pk).exists()


# --- troca ativa: "Registrar a manutenção" já vem com a data da ida à oficina ---------


def test_registrar_com_entrada_preenche_data_e_entrada(usuario_logado, veiculo):
    entrada = HOJE - timedelta(days=12)
    resposta = usuario_logado.get(
        f"/manutencao/registrar/{veiculo.pk}/?entrada={entrada.isoformat()}"
    )
    form = resposta.context["form"]
    assert form.initial["data"] == entrada
    assert form.initial["data_entrada"] == entrada


@pytest.mark.parametrize("texto", ["2026-13-40", "ontem", (HOJE + timedelta(days=3)).isoformat()])
def test_registrar_com_entrada_invalida_ignora(usuario_logado, veiculo, texto):
    resposta = usuario_logado.get(f"/manutencao/registrar/{veiculo.pk}/?entrada={texto}")
    form = resposta.context["form"]
    assert form.initial["data"] == HOJE
    assert "data_entrada" not in form.initial


def test_ficha_com_troca_ativa_manda_a_data_da_retirada(usuario_logado, veiculo):
    from apps.alocacoes.models import Alocacao, TrocaTemporaria
    from apps.pessoas.models import Cliente

    cliente = Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")
    alocacao = Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=30),
        km_entrega=90_000,
        valor_semanal=Decimal("650.00"),
    )
    substituto = Veiculo.objects.create(placa="SEE1E02", marca_modelo="Gol", km_atual=20_000)
    retirada = HOJE - timedelta(days=12)
    TrocaTemporaria.objects.create(
        alocacao=alocacao, veiculo_substituto=substituto, data_retirada=retirada, km_retirada=20_000
    )

    html = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/").content.decode()
    link = f"/manutencao/registrar/{veiculo.pk}/?entrada={retirada.isoformat()}&amp;volta="
    assert link in html


# --- plano: rótulo "Em uso" com altura de toque -----------------------------------------


def test_plano_rotulo_em_uso_tem_44px(usuario_logado, db):
    from apps.manutencao.models import ItemPreventiva

    if not ItemPreventiva.objects.exists():
        ItemPreventiva.objects.create(nome="Óleo", intervalo_km_padrao=10_000)
    html = usuario_logado.get("/manutencao/plano/").content.decode()
    assert '<label class="flex min-h-11 items-center gap-2">' in html
