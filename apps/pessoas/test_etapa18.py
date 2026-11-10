"""Etapa 18: caixa "Remover" do arquivo com tamanho de toque."""

from django.core.files.uploadedfile import SimpleUploadedFile

from apps.pessoas.models import Cliente


def test_caixa_remover_arquivo_tem_tamanho_fixo_e_nao_largura_automatica(usuario_logado, db):
    cliente = Cliente.objects.create(
        nome="Arlen",
        cpf_cnpj="529.982.247-25",
        cnh_frente=SimpleUploadedFile("cnh.pdf", b"%PDF-1.4 teste", "application/pdf"),
    )
    html = usuario_logado.get(f"/clientes/{cliente.pk}/editar/").content.decode()
    assert "Ver arquivo atual" in html
    assert 'name="cnh_frente-clear"' in html
    assert 'style="width:auto"' not in html
    assert "width:1.25rem;height:1.25rem" in html
