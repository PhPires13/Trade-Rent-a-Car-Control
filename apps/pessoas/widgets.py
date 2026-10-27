"""Campo de arquivo simples para leigo: "Ver arquivo atual", "Remover" e "Trocar".

O ClearableFileInput do Django mostra o caminho técnico ("Atualmente:
cnh/arquivo.jpg") e, se a pessoa marca "Limpar" e escolhe um arquivo novo
(o jeito natural de trocar), recusa tudo com um erro confuso. Aqui o
arquivo novo sempre vence: escolher outro já é trocar.
"""

from django import forms
from django.utils.html import format_html


class ArquivoInput(forms.ClearableFileInput):
    def value_from_datadict(self, data, files, name):
        novo = files.get(name)
        if novo:
            # arquivo novo + "Remover" marcado = trocar (não é contradição)
            return novo
        return super().value_from_datadict(data, files, name)

    def render(self, name, value, attrs=None, renderer=None):
        # só o <input type="file"> (o id continua id_<campo>, usado pela leitura da CNH)
        campo = forms.FileInput(self.attrs).render(name, None, attrs, renderer)
        if not self.is_initial(value):
            return campo
        checkbox_id = self.clear_checkbox_id(self.clear_checkbox_name(name))
        remover = ""
        if not self.is_required:
            remover = format_html(
                '<label for="{}" class="inline-flex min-h-11 items-center gap-2 rounded border '
                'border-gray-300 px-3 py-2">'
                '<input type="checkbox" name="{}" id="{}" style="width:auto"> Remover</label>',
                checkbox_id,
                self.clear_checkbox_name(name),
                checkbox_id,
            )
        return format_html(
            '<div class="mb-2 flex flex-wrap items-center gap-2 text-sm">'
            '<a href="{}" target="_blank" rel="noopener" class="inline-flex min-h-11 items-center '
            'rounded border border-gray-300 px-3 py-2 font-medium underline">Ver arquivo atual</a>'
            "{}</div>"
            '<p class="mb-1 text-xs text-gray-600">Para trocar, escolha outro arquivo:</p>{}',
            value.url,
            remover,
            campo,
        )
