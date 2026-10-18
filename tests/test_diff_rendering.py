import io

from rich.console import Console

from faultline.diff_rendering import classify_diff_line, render_unified_diff


DIFF = (
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def value():\n"
    "-    return 1\n"
    "+    return 2\n"
)


def test_classifies_git_diff_lines():
    assert classify_diff_line("--- a/app.py") == "blue"
    assert classify_diff_line("+++ b/app.py") == "blue"
    assert classify_diff_line("@@ -1,2 +1,2 @@") == "cyan"
    assert classify_diff_line("-removed") == "red"
    assert classify_diff_line("+added") == "green"
    assert classify_diff_line(" context") is None


def test_color_rendering_styles_only_diff_lines():
    console = Console(file=io.StringIO(), force_terminal=True, color_system="standard")

    rendered = render_unified_diff(DIFF, console=console)

    assert rendered.plain == DIFF
    styles = {str(span.style) for span in rendered.spans}
    assert {"blue", "cyan", "red", "green"} <= styles


def test_no_color_environment_disables_ansi(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    output = io.StringIO()
    console = Console(file=output, force_terminal=True, color_system="standard")

    rendered = render_unified_diff(DIFF, console=console)
    console.print(rendered)

    assert rendered.plain == DIFF
    assert not rendered.spans
    assert "\x1b[" not in output.getvalue()


def test_non_tty_fallback_is_plain_text():
    output = io.StringIO()
    console = Console(file=output, force_terminal=False)

    rendered = render_unified_diff(DIFF, console=console)
    console.print(rendered)

    assert rendered.plain == DIFF
    assert not rendered.spans
    assert "\x1b[" not in output.getvalue()


def test_dumb_terminal_fallback_is_plain_text(monkeypatch):
    monkeypatch.setenv("TERM", "dumb")
    output = io.StringIO()
    console = Console(file=output, force_terminal=True, color_system="standard")

    rendered = render_unified_diff(DIFF, console=console)
    console.print(rendered)

    assert rendered.plain == DIFF
    assert not rendered.spans
    assert "\x1b[" not in output.getvalue()
