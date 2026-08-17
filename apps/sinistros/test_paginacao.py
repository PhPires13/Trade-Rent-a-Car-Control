"""Paginação da lista de sinistros — fim do corte silencioso em 200 (auditoria UX)."""

from datetime import date, timedelta

from apps.frota.models import Veiculo
from apps.sinistros.models import Sinistro


def test_lista_de_sinistros_paginada(usuario_logado, db):
    veiculo = Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol")
    Sinistro.objects.bulk_create(
        [
            Sinistro(
                veiculo=veiculo,
                data=date(2026, 1, 1) + timedelta(days=i),
                envolvido="terceiro",
            )
            for i in range(30)
        ]
    )
    resposta = usuario_logado.get("/sinistros/")
    assert resposta.context["pagina"].paginator.count == 30
    assert len(resposta.context["sinistros"]) == 25
    html = resposta.content.decode()
    assert "de 30 sinistros" in html
    pagina2 = usuario_logado.get("/sinistros/", {"pagina": "2"})
    assert len(pagina2.context["sinistros"]) == 5
