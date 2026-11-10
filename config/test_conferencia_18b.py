"""Conferência final da etapa 18b: as telas principais não podem crescer com os dados.

Monta uma operação com N carros alugados (cada um com caução, semanas de
aluguel, pagamento parcial, multa, sinistro, manutenção, leitura de km e ND) e
compara o número de consultas de cada tela com 3 e com 25 carros — o que dá
mais de 50 registros por tela (25 carros x ~9 semanas = 200+ cobranças).
Uma tela que consulta "linha a linha" cresce junto e o teste pega.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.financeiro import services as financeiro
from apps.financeiro.models import Cobranca
from apps.frota.models import Veiculo
from apps.km.models import RegistroKm
from apps.manutencao.models import ItemPreventiva, Manutencao
from apps.multas import services as multas_services
from apps.multas.models import Multa
from apps.pessoas.models import Cliente, CondutorAutorizado
from apps.sinistros.models import Sinistro

POUCOS = 3
MUITOS = 25


def _montar(inicio, quantidade):
    """`quantidade` carros alugados há ~9 semanas, com um pouco de tudo."""
    hoje = timezone.localdate()
    oleo = ItemPreventiva.objects.filter(nome="Troca de óleo e filtro").first()
    for i in range(inicio, inicio + quantidade):
        carro = Veiculo.objects.create(
            placa=f"TQC{i:01d}{chr(65 + i % 26)}{i % 100:02d}"[:7],
            marca_modelo="Gol",
            km_atual=60_000,
            km_compra=40_000,
        )
        reserva = Veiculo.objects.create(
            placa=f"RSV{i % 10}{chr(65 + i % 26)}{i % 100:02d}"[:7],
            marca_modelo="Onix",
            km_atual=30_000,
        )
        pessoa = Cliente.objects.create(nome=f"Motorista {i:03d}", cpf_cnpj=f"{i:03d}.222.333-44")
        CondutorAutorizado.objects.create(cliente=pessoa, nome=f"Condutor {i:03d}")
        alocacao = Alocacao.objects.create(
            veiculo=carro,
            cliente=pessoa,
            data_inicio=hoje - timedelta(days=60),
            valor_semanal=Decimal("650.00"),
            km_entrega=50_000,
        )
        financeiro.abrir_caucao(alocacao, Decimal("1000.00"), hoje - timedelta(days=60), "pix")
        financeiro.gerar_cobrancas_semanais(hoje=hoje, alocacao=alocacao)
        primeira = alocacao.cobrancas.order_by("vencimento").first()
        financeiro.registrar_recebimento(
            pessoa,
            hoje - timedelta(days=50),
            Decimal("300.00"),
            "pix",
            [(primeira, Decimal("300.00"))],
        )
        TrocaTemporaria.objects.create(
            alocacao=alocacao,
            veiculo_substituto=reserva,
            data_retirada=hoje - timedelta(days=20),
            km_retirada=30_000,
        )
        multa = Multa.objects.create(
            veiculo=carro,
            cliente=pessoa,
            data_infracao=hoje - timedelta(days=30),
            descricao="Velocidade",
            valor=Decimal("130.16"),
        )
        Multa.objects.create(
            veiculo=carro,
            data_infracao=hoje - timedelta(days=25),
            descricao="Estacionamento",
            valor=Decimal("88.38"),
        )
        multas_services.gerar_nd_de_multas(pessoa, [multa], hoje - timedelta(days=10))
        Sinistro.objects.create(
            veiculo=carro,
            cliente=pessoa,
            data=hoje - timedelta(days=15),
            envolvido="terceiro",
            franquia_valor=Decimal("1500.00"),
        )
        if oleo:
            Manutencao.objects.create(
                veiculo=carro,
                item=oleo,
                tipo="preventiva",
                data=hoje - timedelta(days=40),
                km=52_000,
                descricao="Óleo",
                custo_real=Decimal("300.00"),
            )
        Manutencao.objects.create(
            veiculo=carro,
            tipo="corretiva",
            data=hoje - timedelta(days=12),
            km=58_000,
            descricao="Pastilha",
            custo_real=Decimal("250.00"),
        )
        RegistroKm.objects.create(
            veiculo=carro,
            mes_referencia=hoje.replace(day=1) - timedelta(days=1),
            data_leitura=hoje - timedelta(days=10),
            km=59_000,
        )
    financeiro.marcar_atrasos(hoje=hoje)


TELAS = [
    "/",
    "/alocacoes/",
    "/financeiro/",
    "/financeiro/?status=atrasado",
    "/financeiro/receber/",
    "/financeiro/quem-deve/",
    "/financeiro/nd/",
    "/financeiro/caucoes/",
    "/financeiro/das/",
    "/frota/",
    "/frota/desmobilizacao/",
    "/km/",
    "/manutencao/preventivas/",
    "/manutencao/plano/",
    "/multas/",
    "/multas/gerar-nd/",
    "/clientes/",
    "/buscar/?q=Motorista",
    "/relatorios/",
    "/sinistros/",
    "/alocacoes/nova/",
    "/multas/nova/",
    "/sinistros/novo/",
]


def _contar(cliente_http, url):
    with CaptureQueriesContext(connection) as consultas:
        resposta = cliente_http.get(url)
    assert resposta.status_code == 200, (url, resposta.status_code)
    return len(consultas.captured_queries)


@pytest.mark.django_db
def test_telas_principais_nao_crescem_com_os_dados(usuario_logado):
    _montar(0, POUCOS)
    poucos = {url: _contar(usuario_logado, url) for url in TELAS}
    _montar(POUCOS, MUITOS - POUCOS)
    assert Cobranca.objects.count() > 50
    muitos = {url: _contar(usuario_logado, url) for url in TELAS}
    crescem = {url: (poucos[url], muitos[url]) for url in TELAS if muitos[url] > poucos[url] + 2}
    assert not crescem, f"telas que consultam linha a linha: {crescem}"


def _historico_longo(rotulo, semanas):
    """Um motorista com `semanas` de aluguel: pagamentos, multas, km e oficina."""
    hoje = timezone.localdate()
    oleo = ItemPreventiva.objects.filter(nome="Troca de óleo e filtro").first()
    carro = Veiculo.objects.create(
        placa=f"HIS{rotulo}A00"[:7], marca_modelo="Gol", km_atual=90_000, km_compra=40_000
    )
    pessoa = Cliente.objects.create(nome=f"Histórico {rotulo}", cpf_cnpj=f"9{rotulo}9.222.333-44")
    inicio = hoje - timedelta(weeks=semanas)
    alocacao = Alocacao.objects.create(
        veiculo=carro,
        cliente=pessoa,
        data_inicio=inicio,
        valor_semanal=Decimal("650.00"),
        km_entrega=40_000,
    )
    financeiro.abrir_caucao(alocacao, Decimal("1000.00"), inicio, "pix")
    financeiro.gerar_cobrancas_semanais(hoje=hoje, alocacao=alocacao)
    cobrancas = list(alocacao.cobrancas.order_by("vencimento"))
    for cobranca in cobrancas[: len(cobrancas) // 2]:
        financeiro.registrar_recebimento(
            pessoa, cobranca.vencimento, Decimal("650.00"), "pix", [(cobranca, Decimal("650.00"))]
        )
    multas = [
        Multa.objects.create(
            veiculo=carro,
            cliente=pessoa,
            data_infracao=inicio + timedelta(days=7 * i + 1),
            descricao=f"Infração {i}",
            valor=Decimal("130.16"),
        )
        for i in range(semanas // 2)
    ]
    multas_services.gerar_nd_de_multas(pessoa, multas[: len(multas) // 2], hoje)
    for i in range(semanas // 4):
        Manutencao.objects.create(
            veiculo=carro,
            item=oleo if oleo and i % 2 else None,
            tipo="preventiva" if oleo and i % 2 else "corretiva",
            data=inicio + timedelta(days=28 * i + 3),
            km=40_000 + 2_000 * i,
            descricao=f"Serviço {i}",
            custo_real=Decimal("200.00"),
        )
        RegistroKm.objects.create(
            veiculo=carro,
            mes_referencia=inicio + timedelta(days=31 * i),
            data_leitura=inicio + timedelta(days=31 * i),
            km=40_000 + 2_000 * i,
        )
    financeiro.marcar_atrasos(hoje=hoje)
    return carro, pessoa, alocacao


def _telas_do_historico(carro, pessoa, alocacao):
    return [
        f"/clientes/{pessoa.pk}/",
        f"/financeiro/cliente/{pessoa.pk}/",
        f"/financeiro/receber/?cliente={pessoa.pk}",
        f"/financeiro/caucoes/{alocacao.caucao.pk}/",
        f"/financeiro/?cliente={pessoa.pk}",
        f"/multas/?cliente={pessoa.pk}",
        f"/multas/gerar-nd/?cliente={pessoa.pk}",
        f"/frota/veiculo/{carro.pk}/",
        f"/frota/veiculo/{carro.pk}/ficha/",
        f"/frota/veiculo/{carro.pk}/editar/",
        f"/alocacoes/veiculo/{carro.pk}/linha-do-tempo/",
        f"/alocacoes/{alocacao.pk}/editar/",
        f"/alocacoes/{alocacao.pk}/encerrar/",
        f"/alocacoes/{alocacao.pk}/contrato/",
        f"/km/historico/{carro.pk}/",
        f"/manutencao/historico/{carro.pk}/",
        f"/manutencao/registrar/{carro.pk}/",
    ]


@pytest.mark.django_db
def test_telas_de_um_cliente_ou_carro_nao_crescem_com_o_historico(usuario_logado):
    """Um motorista de 1 ano (52 semanas, 26 multas) custa o mesmo que um de 8 semanas."""
    curto = _telas_do_historico(*_historico_longo("1", 8))
    longo = _telas_do_historico(*_historico_longo("2", 52))
    contagem = {
        a: (_contar(usuario_logado, a), _contar(usuario_logado, b))
        for a, b in zip(curto, longo, strict=True)
    }
    crescem = {url: par for url, par in contagem.items() if par[1] > par[0] + 2}
    assert not crescem, f"telas que consultam linha a linha: {crescem}"


def test_hash_rapido_so_nos_testes(settings):
    """A suíte usa hash de senha rápido; produção segue com o forte (PBKDF2).

    O PBKDF2 custava ~0,17 s por senha, 2x por teste logado: ~7 dos 7,5 minutos
    da suíte. O atalho não pode vazar para o config/settings.py.
    """
    from django.conf import global_settings

    import config.settings as producao

    assert settings.PASSWORD_HASHERS == ["django.contrib.auth.hashers.MD5PasswordHasher"]
    hashers_de_producao = getattr(producao, "PASSWORD_HASHERS", global_settings.PASSWORD_HASHERS)
    assert hashers_de_producao[0] == "django.contrib.auth.hashers.PBKDF2PasswordHasher"
