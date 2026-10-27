"""Pendências da frente multas-sinistros (D16): roubo do carro substituto, roubo
com troca em aberto, botão para aplicar o roubo de novo e sinistro que deixa de
ser roubo."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente
from apps.sinistros.models import Sinistro

HOJE = timezone.localdate()


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="GHI7J80", marca_modelo="HB20", km_atual=50000)


@pytest.fixture
def substituto(db):
    return Veiculo.objects.create(placa="SUB1A23", marca_modelo="Onix", km_atual=30000)


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Pedro", cpf_cnpj="111.222.333-44")


@pytest.fixture
def alocacao(veiculo, cliente):
    veiculo.status = Veiculo.Status.ALOCADO
    veiculo.save(update_fields=["status"])
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=60),
        valor_semanal=Decimal("650.00"),
        km_entrega=40000,
    )


@pytest.fixture
def troca(alocacao, veiculo, substituto):
    veiculo.status = Veiculo.Status.EM_MANUTENCAO
    veiculo.save(update_fields=["status"])
    return TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=HOJE - timedelta(days=10),
        km_retirada=30000,
    )


def _dados(veiculo, **extra):
    dados = {
        "veiculo": veiculo.pk,
        "data": (HOJE - timedelta(days=3)).isoformat(),
        "cliente": "",
        "tipo": Sinistro.Tipo.COLISAO,
        "envolvido": Sinistro.Envolvido.ASSOCIADO,
        "responsabilidade": Sinistro.Responsabilidade.INDEFINIDA,
        "descricao": "",
        "boletim_ocorrencia": "",
        "responsavel_custo": "empresa",
        "status": Sinistro.Status.ABERTO,
        "observacoes": "",
    }
    dados.update(extra)
    return dados


def test_roubo_do_substituto_encerra_a_troca_e_inativa_o_substituto(
    usuario_logado, alocacao, troca, substituto
):
    data_roubo = HOJE - timedelta(days=3)
    resposta = usuario_logado.post(
        "/sinistros/novo/",
        _dados(substituto, tipo=Sinistro.Tipo.ROUBO, data=data_roubo.isoformat(), token="p1"),
        follow=True,
    )
    html = resposta.content.decode()
    assert "era o carro substituto de Pedro" in html
    troca.refresh_from_db()
    substituto.refresh_from_db()
    alocacao.refresh_from_db()
    assert troca.data_devolucao == data_roubo
    assert troca.km_devolucao == 30000
    assert substituto.status == Veiculo.Status.INATIVO
    assert alocacao.status == Alocacao.Status.ATIVA  # o dono decide a locação
    assert Sinistro.objects.get().cliente == alocacao.cliente


def test_roubo_do_carro_parado_com_troca_aberta_mantem_a_locacao(
    usuario_logado, alocacao, troca, veiculo, substituto
):
    resposta = usuario_logado.post(
        "/sinistros/novo/", _dados(veiculo, tipo=Sinistro.Tipo.ROUBO, token="p2"), follow=True
    )
    html = resposta.content.decode()
    assert "continua, porque ele está com o carro substituto SUB1A23" in html
    alocacao.refresh_from_db()
    troca.refresh_from_db()
    veiculo.refresh_from_db()
    substituto.refresh_from_db()
    assert alocacao.status == Alocacao.Status.ATIVA
    assert troca.data_devolucao is None
    assert veiculo.status == Veiculo.Status.INATIVO
    assert substituto.status == Veiculo.Status.ALOCADO


def test_roubo_antigo_do_substituto_nao_mexe_na_troca(usuario_logado, troca, substituto):
    data_roubo = troca.data_retirada - timedelta(days=5)
    resposta = usuario_logado.post(
        "/sinistros/novo/",
        _dados(substituto, tipo=Sinistro.Tipo.ROUBO, data=data_roubo.isoformat(), token="p3"),
        follow=True,
    )
    assert "Aplicar o roubo" in resposta.content.decode()
    troca.refresh_from_db()
    substituto.refresh_from_db()
    assert troca.data_devolucao is None
    assert substituto.status == Veiculo.Status.ALOCADO


def test_botao_aplicar_o_roubo_refaz_o_encerramento(usuario_logado, alocacao, veiculo):
    # roubo salvo com data errada (antes da locação): nada foi encerrado
    sinistro = Sinistro.objects.create(
        veiculo=veiculo,
        data=alocacao.data_inicio - timedelta(days=1),
        tipo=Sinistro.Tipo.ROUBO,
        envolvido="associado",
    )
    url_editar = f"/sinistros/{sinistro.pk}/editar/"
    html = usuario_logado.get(url_editar).content.decode()
    assert "Aplicar o roubo" in html
    # a pessoa corrige a data (sem mudar o tipo) e usa o botão
    sinistro.data = HOJE - timedelta(days=2)
    sinistro.save(update_fields=["data"])
    usuario_logado.post(f"/sinistros/{sinistro.pk}/aplicar-roubo/", {"volta": url_editar})
    alocacao.refresh_from_db()
    veiculo.refresh_from_db()
    assert alocacao.status == Alocacao.Status.ENCERRADA
    assert alocacao.data_termino == sinistro.data
    assert veiculo.status == Veiculo.Status.INATIVO
    html = usuario_logado.get(url_editar).content.decode()
    assert "Aplicar o roubo" not in html
    assert "Carro recuperado" in html


def test_aplicar_o_roubo_em_sinistro_que_nao_e_roubo_nao_faz_nada(
    usuario_logado, alocacao, veiculo
):
    sinistro = Sinistro.objects.create(
        veiculo=veiculo, data=HOJE - timedelta(days=2), envolvido="associado"
    )
    usuario_logado.post(f"/sinistros/{sinistro.pk}/aplicar-roubo/")
    alocacao.refresh_from_db()
    assert alocacao.status == Alocacao.Status.ATIVA
    assert usuario_logado.get(f"/sinistros/{sinistro.pk}/aplicar-roubo/").status_code == 302


def test_deixar_de_ser_roubo_devolve_o_carro_a_disponivel(usuario_logado, alocacao, veiculo):
    usuario_logado.post("/sinistros/novo/", _dados(veiculo, tipo=Sinistro.Tipo.ROUBO, token="p4"))
    sinistro = Sinistro.objects.get()
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.INATIVO
    resposta = usuario_logado.post(
        f"/sinistros/{sinistro.pk}/editar/",
        _dados(veiculo, tipo=Sinistro.Tipo.COLISAO, data=sinistro.data.isoformat()),
        follow=True,
    )
    assert "voltou a ficar Disponível" in resposta.content.decode()
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.DISPONIVEL


def test_deixar_de_ser_roubo_nao_libera_carro_com_outro_roubo_aberto(usuario_logado, veiculo):
    veiculo.status = Veiculo.Status.INATIVO
    veiculo.save(update_fields=["status"])
    Sinistro.objects.create(
        veiculo=veiculo, data=HOJE - timedelta(days=9), tipo="roubo", envolvido="associado"
    )
    errado = Sinistro.objects.create(
        veiculo=veiculo, data=HOJE - timedelta(days=8), tipo="roubo", envolvido="associado"
    )
    usuario_logado.post(
        f"/sinistros/{errado.pk}/editar/",
        _dados(veiculo, tipo=Sinistro.Tipo.OUTRO, data=errado.data.isoformat()),
    )
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.INATIVO


def test_roubo_ja_recuperado_que_muda_de_tipo_nao_mexe_no_carro(usuario_logado, veiculo):
    veiculo.status = Veiculo.Status.INATIVO  # inativo por outro motivo
    veiculo.save(update_fields=["status"])
    sinistro = Sinistro.objects.create(
        veiculo=veiculo,
        data=HOJE - timedelta(days=9),
        tipo="roubo",
        envolvido="associado",
        data_recuperacao=HOJE - timedelta(days=5),
    )
    usuario_logado.post(
        f"/sinistros/{sinistro.pk}/editar/",
        _dados(veiculo, tipo=Sinistro.Tipo.OUTRO, data=sinistro.data.isoformat()),
    )
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.INATIVO


def test_evento_da_protecao_antes_do_ocorrido_e_recusado(usuario_logado, veiculo):
    ocorrido = HOJE - timedelta(days=3)
    resposta = usuario_logado.post(
        "/sinistros/novo/",
        _dados(
            veiculo,
            data=ocorrido.isoformat(),
            acionou_protecao="on",
            data_evento=(ocorrido - timedelta(days=30)).isoformat(),
            token="p5",
        ),
    )
    assert resposta.status_code == 200
    assert "antes do ocorrido" in resposta.content.decode()
    assert not Sinistro.objects.exists()


def test_colisao_tem_atalho_para_registrar_ida_a_oficina(usuario_logado, veiculo):
    sinistro = Sinistro.objects.create(
        veiculo=veiculo, data=HOJE - timedelta(days=2), envolvido="associado"
    )
    link = f"/manutencao/registrar/{veiculo.pk}/?sinistro={sinistro.pk}"
    assert link in usuario_logado.get("/sinistros/").content.decode()
    assert link in usuario_logado.get(f"/sinistros/{sinistro.pk}/editar/").content.decode()
    assert usuario_logado.get(link).status_code == 200


# --- revisão: carro roubado parado com troca aberta não pode voltar "Disponível"


def test_recuperar_carro_da_locacao_com_troca_aberta_nao_fica_disponivel(
    usuario_logado, alocacao, troca, veiculo
):
    usuario_logado.post("/sinistros/novo/", _dados(veiculo, tipo=Sinistro.Tipo.ROUBO, token="r1"))
    sinistro = Sinistro.objects.get()
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.INATIVO
    resposta = usuario_logado.post(f"/sinistros/{sinistro.pk}/recuperado/", follow=True)
    veiculo.refresh_from_db()
    # ainda é o carro da locação ativa: não pode aparecer para alugar a outro
    assert veiculo.status == Veiculo.Status.ALOCADO
    assert "agora é Alocado" in resposta.content.decode()


def test_deixar_de_ser_roubo_com_locacao_ativa_volta_alocado(
    usuario_logado, alocacao, troca, veiculo
):
    usuario_logado.post("/sinistros/novo/", _dados(veiculo, tipo=Sinistro.Tipo.ROUBO, token="r2"))
    sinistro = Sinistro.objects.get()
    usuario_logado.post(
        f"/sinistros/{sinistro.pk}/editar/",
        _dados(veiculo, tipo=Sinistro.Tipo.COLISAO, data=sinistro.data.isoformat()),
    )
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.ALOCADO


def test_recuperar_carro_sem_locacao_volta_disponivel(usuario_logado, veiculo):
    veiculo.status = Veiculo.Status.INATIVO
    veiculo.save(update_fields=["status"])
    sinistro = Sinistro.objects.create(
        veiculo=veiculo, data=HOJE - timedelta(days=4), tipo="roubo", envolvido="associado"
    )
    usuario_logado.post(f"/sinistros/{sinistro.pk}/recuperado/")
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.DISPONIVEL


# --- revisão: roubo lançado no carro errado e corrigido na edição


def test_trocar_o_carro_do_roubo_libera_o_antigo_e_aplica_no_novo(
    usuario_logado, alocacao, veiculo, substituto
):
    # roubo lançado por engano no carro parado (sem locação)
    substituto.status = Veiculo.Status.INATIVO
    substituto.save(update_fields=["status"])
    sinistro = Sinistro.objects.create(
        veiculo=substituto, data=HOJE - timedelta(days=2), tipo="roubo", envolvido="associado"
    )
    resposta = usuario_logado.post(
        f"/sinistros/{sinistro.pk}/editar/",
        _dados(veiculo, tipo=Sinistro.Tipo.ROUBO, data=sinistro.data.isoformat()),
        follow=True,
    )
    html = resposta.content.decode()
    assert "o roubo é de outro carro, SUB1A23 voltou a ficar Disponível" in html
    substituto.refresh_from_db()
    veiculo.refresh_from_db()
    alocacao.refresh_from_db()
    assert substituto.status == Veiculo.Status.DISPONIVEL
    assert veiculo.status == Veiculo.Status.INATIVO
    assert alocacao.status == Alocacao.Status.ENCERRADA
    assert alocacao.data_termino == sinistro.data


def test_roubo_que_vira_colisao_em_outro_carro_libera_o_carro_antigo(
    usuario_logado, veiculo, substituto
):
    substituto.status = Veiculo.Status.INATIVO
    substituto.save(update_fields=["status"])
    sinistro = Sinistro.objects.create(
        veiculo=substituto, data=HOJE - timedelta(days=2), tipo="roubo", envolvido="associado"
    )
    usuario_logado.post(
        f"/sinistros/{sinistro.pk}/editar/",
        _dados(veiculo, tipo=Sinistro.Tipo.COLISAO, data=sinistro.data.isoformat()),
    )
    substituto.refresh_from_db()
    veiculo.refresh_from_db()
    assert substituto.status == Veiculo.Status.DISPONIVEL
    assert veiculo.status == Veiculo.Status.DISPONIVEL
