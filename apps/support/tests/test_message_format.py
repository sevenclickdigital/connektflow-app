"""The support messages' formatting marks: escaped first, then only a few fixed tags.

``EXPECTED`` is what the JavaScript twin (support/partials/_message_format.html)
renders for the same texts, checked in the browser: the two must agree.
"""

import pytest

from apps.support.templatetags.support_tags import message_format

EXPECTED = [
    ("Abra o *Calendário* e clique em _Agendar_.", "Abra o <strong>Calendário</strong> e clique em <em>Agendar</em>."),
    (
        "~antigo~ `codigo` https://a.com/x_y.",
        '<s>antigo</s> <code>codigo</code> <a href="https://a.com/x_y" target="_blank" rel="noopener noreferrer" '
        'class="underline">https://a.com/x_y</a>.',
    ),
    ("2*3*4 e snake_case_word", "2*3*4 e snake_case_word"),
    ('(*ok*) "_aspas_"', "(<strong>ok</strong>) &quot;_aspas_&quot;"),
    ('<b>x</b> & "y"', "&lt;b&gt;x&lt;/b&gt; &amp; &quot;y&quot;"),
    (
        'https://a.com/"onmouseover=alert(1)',
        '<a href="https://a.com/&quot;onmouseover=alert(1" target="_blank" rel="noopener noreferrer" '
        'class="underline">https://a.com/&quot;onmouseover=alert(1</a>)',
    ),
]


@pytest.mark.parametrize(("text", "html"), EXPECTED)
def test_the_marks_become_tags_and_nothing_else_does(text, html):
    assert message_format(text) == html


def test_a_script_or_a_javascript_link_stays_text():
    html = message_format("<script>alert(1)</script> javascript:alert(1) *<img src=x onerror=alert(1)>*")

    assert "<script" not in html and "<img" not in html and "href" not in html
