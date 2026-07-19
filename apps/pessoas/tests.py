import pytest
from django.db import IntegrityError

from apps.pessoas.models import Cliente


@pytest.mark.django_db
def test_cpf_cnpj_unico():
    Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")
    with pytest.raises(IntegrityError):
        Cliente.objects.create(nome="Outro", cpf_cnpj="111.222.333-44")


@pytest.mark.django_db
def test_cliente_nasce_ativo():
    cliente = Cliente.objects.create(nome="Welington", cpf_cnpj="555.666.777-88")
    assert cliente.status == Cliente.Status.ATIVO
