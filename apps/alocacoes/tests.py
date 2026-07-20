from datetime import date

import pytest
from django.core.exceptions import ValidationError

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.alocacoes.services import cliente_vigente, linha_do_tempo
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_atual=50_000)


@pytest.fixture
def substituto(db):
    return Veiculo.objects.create(placa="RUJ3I28", marca_modelo="Gol", km_atual=40_000)


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


def alocar(veiculo, cliente, inicio=date(2026, 7, 1), valor=650, km=50_000):
    alocacao = Alocacao(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=inicio,
        valor_semanal=valor,
        dia_vencimento=inicio.weekday(),
        km_entrega=km,
    )
    alocacao.full_clean()
    alocacao.save()
    return alocacao


def test_alocar_muda_status_do_veiculo(veiculo, cliente):
    alocar(veiculo, cliente)
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.ALOCADO


def test_veiculo_indisponivel_nao_pode_ser_alocado(veiculo, cliente, db):
    alocar(veiculo, cliente)
    outro = Cliente.objects.create(nome="Lucas", cpf_cnpj="999.888.777-66")
    veiculo.refresh_from_db()
    with pytest.raises(ValidationError):
        alocar(veiculo, outro)


def test_veiculo_fora_de_locacao_nao_pode_ser_alocado(cliente, db):
    pessoal = Veiculo.objects.create(
        placa="RVZ9J95", marca_modelo="HB20", uso=Veiculo.Uso.FORA_LOCACAO
    )
    with pytest.raises(ValidationError):
        alocar(pessoal, cliente)


def test_dia_vencimento_padrao_eh_o_da_entrega(veiculo, cliente):
    alocacao = Alocacao(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),  # quarta-feira
        valor_semanal=650,
        km_entrega=50_000,
    )
    alocacao.save()
    assert alocacao.dia_vencimento == 2


def test_encerrar_libera_veiculo_e_registra_km(veiculo, cliente):
    alocacao = alocar(veiculo, cliente)
    alocacao.encerrar(data_termino=date(2026, 7, 20), km_devolucao=52_000)
    veiculo.refresh_from_db()
    assert alocacao.status == Alocacao.Status.ENCERRADA
    assert veiculo.status == Veiculo.Status.DISPONIVEL
    assert veiculo.km_atual == 52_000


def test_nao_encerra_com_km_menor_que_entrega(veiculo, cliente):
    alocacao = alocar(veiculo, cliente)
    with pytest.raises(ValidationError):
        alocacao.encerrar(data_termino=date(2026, 7, 20), km_devolucao=49_000)


def trocar(alocacao, substituto, retirada=date(2026, 7, 10), km=40_000, valor=None):
    troca = TrocaTemporaria(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=retirada,
        km_retirada=km,
        valor_semanal_ajustado=valor,
    )
    troca.full_clean()
    troca.save()
    return troca


def test_troca_aloca_substituto(veiculo, substituto, cliente):
    alocacao = alocar(veiculo, cliente)
    trocar(alocacao, substituto)
    substituto.refresh_from_db()
    assert substituto.status == Veiculo.Status.ALOCADO


def test_uma_troca_ativa_por_alocacao(veiculo, substituto, cliente, db):
    alocacao = alocar(veiculo, cliente)
    trocar(alocacao, substituto)
    terceiro = Veiculo.objects.create(placa="RNB9J66", marca_modelo="Voyage")
    with pytest.raises(ValidationError):
        trocar(alocacao, terceiro)


def test_devolucao_libera_substituto(veiculo, substituto, cliente):
    alocacao = alocar(veiculo, cliente)
    troca = trocar(alocacao, substituto)
    troca.devolver(data_devolucao=date(2026, 7, 14), km_devolucao=41_200)
    substituto.refresh_from_db()
    assert substituto.status == Veiculo.Status.DISPONIVEL
    assert substituto.km_atual == 41_200


def test_nao_encerra_alocacao_com_troca_em_andamento(veiculo, substituto, cliente):
    alocacao = alocar(veiculo, cliente)
    trocar(alocacao, substituto)
    with pytest.raises(ValidationError):
        alocacao.encerrar(data_termino=date(2026, 7, 20), km_devolucao=52_000)


def test_cliente_vigente_considera_trocas(veiculo, substituto, cliente):
    alocacao = alocar(veiculo, cliente)
    troca = trocar(alocacao, substituto, retirada=date(2026, 7, 10))
    troca.devolver(data_devolucao=date(2026, 7, 14), km_devolucao=41_200)

    # substituto no período da troca → cliente da alocação
    assert cliente_vigente(substituto, date(2026, 7, 12)) == cliente
    # substituto fora do período → ninguém
    assert cliente_vigente(substituto, date(2026, 7, 20)) is None
    # carro principal segue vinculado ao cliente mesmo durante o conserto
    assert cliente_vigente(veiculo, date(2026, 7, 12)) == cliente
    # antes da alocação → ninguém
    assert cliente_vigente(veiculo, date(2026, 6, 1)) is None


def test_cliente_vigente_apos_encerramento(veiculo, cliente):
    alocacao = alocar(veiculo, cliente)
    alocacao.encerrar(data_termino=date(2026, 7, 20), km_devolucao=52_000)
    assert cliente_vigente(veiculo, date(2026, 7, 15)) == cliente
    assert cliente_vigente(veiculo, date(2026, 7, 25)) is None


def test_linha_do_tempo_reune_eventos(veiculo, substituto, cliente):
    alocacao = alocar(veiculo, cliente)
    troca = trocar(alocacao, substituto, retirada=date(2026, 7, 10))
    troca.devolver(data_devolucao=date(2026, 7, 14), km_devolucao=41_200)

    eventos_principal = linha_do_tempo(veiculo)
    tipos = [e["tipo"] for e in eventos_principal]
    assert "Alocação" in tipos
    assert "Carro na oficina" in tipos

    eventos_substituto = linha_do_tempo(substituto)
    tipos_substituto = [e["tipo"] for e in eventos_substituto]
    assert "Troca temporária" in tipos_substituto
    assert "Fim da troca" in tipos_substituto


@pytest.fixture
def usuario_logado(client, django_user_model):
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    client.login(username="dono", password="senha-forte-123")
    return client


def test_telas_de_alocacao_renderizam(usuario_logado, veiculo, substituto, cliente):
    alocacao = alocar(veiculo, cliente)
    troca = trocar(alocacao, substituto)
    assert usuario_logado.get("/alocacoes/").status_code == 200
    assert usuario_logado.get("/alocacoes/nova/").status_code == 200
    assert usuario_logado.get(f"/alocacoes/{alocacao.pk}/encerrar/").status_code == 200
    assert usuario_logado.get(f"/alocacoes/troca/{troca.pk}/devolver/").status_code == 200
    assert usuario_logado.get(f"/alocacoes/veiculo/{veiculo.pk}/linha-do-tempo/").status_code == 200


def test_criar_alocacao_pela_tela(usuario_logado, veiculo, cliente):
    resposta = usuario_logado.post(
        "/alocacoes/nova/",
        {
            "veiculo": veiculo.pk,
            "cliente": cliente.pk,
            "data_inicio": "2026-07-01",
            "valor_semanal": "650",
            "km_entrega": "50000",
            "limite_km": "ilimitado",
        },
    )
    assert resposta.status_code == 302
    assert Alocacao.objects.filter(veiculo=veiculo, status="ativa").exists()
