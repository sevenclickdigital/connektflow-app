"""The Markdown the team writes becomes our layout, and nothing a value or the text holds can run."""

from apps.email_center.markdown_mail import render, variables_used


def test_title_button_panel_and_list():
    result = render(
        "# Olá {{ name }}\n\n> Dica\n\n- um\n- dois\n\n[botão: Começar]({{ url }})",
        {"name": "Ana", "url": "https://app.example.com/x"},
    )

    assert result.heading == "Olá Ana"
    assert "background-color:#F5F3FF" in result.html and "<li" in result.html
    assert 'class="cf-btn"' in result.html and "Começar" in result.html
    assert "Começar: https://app.example.com/x" in result.text


def test_markup_written_in_the_text_is_removed():
    result = render("Oi <script>alert(1)</script><img src=x onerror=alert(1)> [a](javascript:alert(1))", {})

    assert "<script" not in result.html
    assert "onerror" not in result.html
    assert "<img" not in result.html
    assert "javascript:" not in result.html


def test_a_value_cannot_add_markup_or_formatting():
    result = render("# {{ name }}\n\nOlá {{ name }}", {"name": "**Ana** <b>x</b> & [y](https://evil)"})

    assert "<b>x</b>" not in result.html and "<strong" not in result.html
    assert "&lt;b&gt;x&lt;/b&gt;" in result.html
    assert 'href="https://evil"' not in result.html
    assert result.heading == "**Ana** &lt;b&gt;x&lt;/b&gt; &amp; [y](https://evil)"


def test_a_value_used_as_a_link_must_be_a_web_address():
    result = render(
        "[site]({{ org }}) [ok]({{ url }})", {"org": "javascript:alert(1)", "url": "https://a.example/?b=1&c=2"}
    )

    assert 'href="#"' in result.html
    assert 'href="https://a.example/?b=1&amp;c=2"' in result.html


def test_variables_used_reads_subject_and_body():
    assert variables_used("Oi {{ name }}", "{{name}} e {{ link_url }}") == {"name", "link_url"}


def test_the_layout_blocks_match_the_default_templates():
    result = render(
        "> passos\n\n`482913`\n\n<small>nota</small>\n\n"
        "| Valor | R$ 150,00 |\n|---|---|\n| Chave Pix | ana |\n\n"
        "> <small>**Origem**\n> Data: hoje</small>\n\n"
        '[botão: Ir](https://a.example/ "sem link")\n\nDepois do botão.',
        {},
    )

    assert "padding:16px 18px;background-color:#F5F3FF" in result.html  # panel
    assert "letter-spacing:6px" in result.html and ">482913</p>" in result.html  # code box
    assert 'font-size:13px;line-height:20px;color:#64748B;">nota</p>' in result.html  # note
    assert "border:1px solid #E9E5FB" in result.html and "text-transform:uppercase" in result.html  # card
    assert "padding:14px 18px;background-color:#F5F3FF" in result.html  # compact box
    assert "Se o botão não funcionar" not in result.html and "If the button" not in result.html
    assert "height:24px" in result.html  # the gap after a button
    assert result.html.rstrip().endswith(
        '<p style="margin:0;font-size:15px;line-height:24px;color:#334155;">Depois do botão.</p>'
    )


def test_the_line_under_a_button_is_the_team_s_text():
    result = render(
        "[botão: Ir]({{ url }})\n\n<small>Não abriu? Use:\n[{{ url }}]({{ url }})</small>",
        {"url": "https://a.example/x"},
    )

    assert 'class="cf-btn-fallback"' in result.html and "Não abriu? Use:" in result.html
    assert "word-break:break-all" in result.html
    assert "Se o botão não funcionar" not in result.html
    assert result.text.endswith("Não abriu? Use:\nhttps://a.example/x")


def test_a_button_alone_adds_no_line_of_its_own():
    result = render("[botão: Ir](https://a.example/x)", {})

    assert 'class="cf-btn"' in result.html
    assert "cf-btn-fallback" not in result.html
