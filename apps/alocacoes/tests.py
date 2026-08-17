from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

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


def test_devolucao_preserva_status_vendido_do_substituto(veiculo, substituto, cliente):
    alocacao = alocar(veiculo, cliente)
    troca = trocar(alocacao, substituto)
    # venda registrada por fora (ex.: Admin) enquanto emprestado — devolver não reabilita
    substituto.status = Veiculo.Status.VENDIDO
    substituto.save(update_fields=["status"])
    troca.devolver(data_devolucao=date(2026, 7, 14), km_devolucao=41_200)
    substituto.refresh_from_db()
    assert substituto.status == Veiculo.Status.VENDIDO


def test_constraint_impede_segunda_troca_aberta_na_alocacao(veiculo, substituto, cliente, db):
    alocacao = alocar(veiculo, cliente)
    trocar(alocacao, substituto)
    terceiro = Veiculo.objects.create(placa="RNB9J66", marca_modelo="Voyage")
    with pytest.raises(IntegrityError):  # direto no banco, sem full_clean (ex.: Admin/shell)
        TrocaTemporaria.objects.create(
            alocacao=alocacao,
            veiculo_substituto=terceiro,
            data_retirada=date(2026, 7, 12),
            km_retirada=0,
        )


def test_constraint_impede_substituto_em_duas_trocas_abertas(veiculo, substituto, cliente, db):
    alocacao = alocar(veiculo, cliente)
    trocar(alocacao, substituto)
    outro_carro = Veiculo.objects.create(placa="RNB9J66", marca_modelo="Voyage")
    outro_cliente = Cliente.objects.create(nome="Lucas", cpf_cnpj="999.888.777-66")
    outra_alocacao = alocar(outro_carro, outro_cliente)
    with pytest.raises(IntegrityError):
        TrocaTemporaria.objects.create(
            alocacao=outra_alocacao,
            veiculo_substituto=substituto,
            data_retirada=date(2026, 7, 12),
            km_retirada=0,
        )


def test_reabrir_troca_devolvida_exige_alocacao_ativa(veiculo, substituto, cliente):
    alocacao = alocar(veiculo, cliente)
    troca = trocar(alocacao, substituto)
    troca.devolver(data_devolucao=date(2026, 7, 14), km_devolucao=41_200)
    alocacao.encerrar(data_termino=date(2026, 7, 20), km_devolucao=52_000)
    troca.data_devolucao = None  # edição via Admin tentando reabrir a troca
    with pytest.raises(ValidationError):
        troca.full_clean()


def test_edicao_nao_encerra_alocacao_sem_data_termino(veiculo, cliente):
    alocacao = alocar(veiculo, cliente)
    alocacao.status = Alocacao.Status.ENCERRADA  # edição via Admin sem data/KM de devolução
    with pytest.raises(ValidationError):
        alocacao.full_clean()


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


def test_troca_ativa_aproveita_o_prefetch(django_assert_max_num_queries, db):
    """Revisão de performance: .filter() na property refazia a consulta por linha."""
    devolvidas = []
    for indice in range(10):
        carro = Veiculo.objects.create(
            placa=f"TQ{indice:02d}A{indice:02d}", marca_modelo="Gol", km_atual=50_000
        )
        reserva = Veiculo.objects.create(
            placa=f"RS{indice:02d}B{indice:02d}", marca_modelo="Voyage", km_atual=40_000
        )
        pessoa = Cliente.objects.create(
            nome=f"Cliente {indice}", cpf_cnpj=f"111.222.333-{indice:02d}"
        )
        alocacao = alocar(carro, pessoa)
        troca = trocar(alocacao, reserva, retirada=date(2026, 7, 10))
        if indice % 2:  # metade devolvida: a property tem de devolver None nelas
            troca.devolver(data_devolucao=date(2026, 7, 14), km_devolucao=41_200)
            devolvidas.append(alocacao.pk)

    with django_assert_max_num_queries(2):
        alocacoes = list(Alocacao.objects.prefetch_related("trocas").order_by("pk"))
        abertas = {a.pk: a.troca_ativa for a in alocacoes}

    assert len(abertas) == 10
    for alocacao_id, troca in abertas.items():
        if alocacao_id in devolvidas:
            assert troca is None
        else:
            assert troca is not None and troca.data_devolucao is None


def test_troca_ativa_sem_prefetch_continua_funcionando(veiculo, substituto, cliente):
    alocacao = alocar(veiculo, cliente)
    troca = trocar(alocacao, substituto)
    assert Alocacao.objects.get(pk=alocacao.pk).troca_ativa == troca
    troca.devolver(data_devolucao=date(2026, 7, 14), km_devolucao=41_200)
    assert Alocacao.objects.get(pk=alocacao.pk).troca_ativa is None


def test_lista_de_alocacoes_com_queries_fixas(usuario_logado, django_assert_max_num_queries, db):
    """A lista mostra a troca de cada linha — não pode voltar a consultar por linha."""
    for indice in range(10):
        carro = Veiculo.objects.create(
            placa=f"TQ{indice:02d}A{indice:02d}", marca_modelo="Gol", km_atual=50_000
        )
        reserva = Veiculo.objects.create(
            placa=f"RS{indice:02d}B{indice:02d}", marca_modelo="Voyage", km_atual=40_000
        )
        pessoa = Cliente.objects.create(
            nome=f"Cliente {indice}", cpf_cnpj=f"111.222.333-{indice:02d}"
        )
        trocar(alocar(carro, pessoa), reserva, retirada=date(2026, 7, 10))
    # eram 40 queries (3 por linha na property); o que sobra por linha é a placa
    # do substituto, que o template busca pela FK da troca prefetchada
    with django_assert_max_num_queries(15):
        resposta = usuario_logado.get("/alocacoes/")
    assert resposta.status_code == 200
    assert "RS00B00" in resposta.content.decode()  # placa do substituto na linha


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


def test_encerrar_com_caucao_retida_leva_ao_acerto(usuario_logado, veiculo, cliente):
    from apps.financeiro import services as financeiro

    alocacao = Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=50_000,
        caucao_valor=Decimal("1000.00"),
    )
    financeiro.abrir_caucao(alocacao, valor_recebido=Decimal("1000.00"), data=date(2026, 7, 1))
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/encerrar/",
        {"data_termino": "2026-07-20", "km_devolucao": "52000"},
    )
    assert resposta.status_code == 302
    # encerrar dispara o acerto de caução (docs.md §4.2): vai direto para a tela da caução
    assert resposta.url == f"/financeiro/caucoes/{alocacao.caucao.pk}/"


def test_encerrar_sem_caucao_volta_para_lista(usuario_logado, veiculo, cliente):
    alocacao = alocar(veiculo, cliente)
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/encerrar/",
        {"data_termino": "2026-07-20", "km_devolucao": "52000"},
    )
    assert resposta.status_code == 302
    assert resposta.url == "/alocacoes/"
