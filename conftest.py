"""Fixtures compartilhadas por toda a suíte."""

import pytest


@pytest.fixture
def usuario_logado(client, django_user_model):
    """Cliente HTTP já autenticado — todas as telas exigem login (docs.md §6)."""
    django_user_model.objects.create_user(username="dono", password="senha-forte-123")
    client.login(username="dono", password="senha-forte-123")
    return client
