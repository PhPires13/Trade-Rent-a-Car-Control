"""Formulários de multas — usados pelas telas e também pelo /admin.

Ficam aqui (e não em views.py) porque o admin precisa exatamente do mesmo
tratamento de credenciais: um form próprio no admin já devolveu a senha do
órgão em claro no HTML (revisão de segurança).
"""

from django import forms

from .models import Multa, OrgaoAutuador


class MultaForm(forms.ModelForm):
    class Meta:
        model = Multa
        fields = [
            "veiculo",
            "data_infracao",
            "cliente",
            "codigo",
            "ait",
            "num_processamento",
            "orgao",
            "descricao",
            "valor",
            "pontos",
            "tipo_condutor",
            "condutor_autorizado",
            "fici_prazo",
            "responsavel",
            "observacoes",
        ]
        widgets = {
            "data_infracao": forms.DateInput(attrs={"type": "date"}),
            "fici_prazo": forms.DateInput(attrs={"type": "date"}),
            "observacoes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["cliente"].required = False
        self.fields[
            "cliente"
        ].help_text = "Deixe vazio para preencher com quem estava com o carro na data"


class OrgaoForm(forms.ModelForm):
    """Cadastro do órgão com portal, credenciais e procedimento (docs.md §4.1).

    A senha nunca volta no HTML da edição (ficaria em claro no fonte e no
    cache do navegador — revisão etapa 8); campo vazio mantém a senha atual.

    O login continua visível: é o identificador do cadastro no portal, não o
    segredo — quem opera precisa conferir com qual usuário está entrando, e a
    tela de órgãos também o mostra. O que não pode aparecer é a senha.
    """

    class Meta:
        model = OrgaoAutuador
        fields = [
            "nome",
            "esfera",
            "portal",
            "login",
            "senha",
            "email",
            "telefone",
            "procedimento",
            "endereco",
            "observacoes",
        ]
        widgets = {
            "login": forms.TextInput(),
            "senha": forms.PasswordInput(),
            "procedimento": forms.Textarea(attrs={"rows": 3}),
            "endereco": forms.Textarea(attrs={"rows": 3}),
            "observacoes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["senha"].required = False
        if self.instance.pk and self.instance.senha:
            self.fields["senha"].help_text = "Deixe em branco para manter a senha atual."

    def clean_senha(self):
        senha = self.cleaned_data.get("senha")
        if not senha and self.instance.pk:
            return self.instance.senha
        return senha
