"""BL-025: `tech_report._decode_partial`, el decodificador incremental del JSON que llega de a pedazos mientras el modelo escribe el codigo (narracion en vivo).

Regla de diseno que estos tests FIJAN (antes solo la sostenia un `except ValueError: pass` sin explicacion): la narracion es de MEJOR ESFUERZO. Un escape `\\uXXXX`
mal formado se OMITE y se sigue (no se inventa un caracter ni se corta la narracion), y una secuencia cortada a la mitad se deja para el proximo fragmento.
Los escapes se arman con `chr(92)` para que ninguna herramienta ni editor los reinterprete."""

from __future__ import annotations

import pytest

from app.tech_report import _decode_partial

BS = chr(92)
Q = chr(34)


def u(hex4: str) -> str:
    return BS + "u" + hex4


def run_bounded(raw: str, start: int = 0, seconds: float = 20.0):
    """Ejecuta `_decode_partial` en un SUBPROCESO con limite de tiempo. Devuelve (termino, resultado).

    Un hilo de Python que gira en un bucle puro no se puede matar y retiene el GIL (se probo: el limite no se respetaba y la suite esperaba al timeout
    general de 60 s). Un subproceso si se termina, y el test FALLA en segundos en vez de colgar la suite."""
    import json
    import subprocess
    import sys

    code = "; ".join(
        [
            "import json, sys",
            "from app.tech_report import _decode_partial",
            "raw, start = json.loads(sys.stdin.read())",
            "print(json.dumps(list(_decode_partial(raw, start))))",
        ]
    )
    try:
        done = subprocess.run(
            [sys.executable, "-c", code],
            input=json.dumps([raw, start]),
            capture_output=True,
            text=True,
            timeout=seconds,
            check=True,
        )
    except subprocess.TimeoutExpired:
        return False, None
    return True, tuple(json.loads(done.stdout))


class TestComplete:
    def test_plain_text_until_the_closing_quote(self):
        assert _decode_partial("hola" + Q + ' , "x"', 0) == ("hola", 5, True)

    def test_it_starts_where_it_is_told(self):
        assert _decode_partial("xxhola" + Q, 2) == ("hola", 7, True)

    @pytest.mark.parametrize(
        "esc,expected",
        [("n", "\n"), ("t", "\t"), ("/", "/"), (BS, BS), (Q, Q), ("r", ""), ("b", ""), ("f", "")],
    )
    def test_known_escapes(self, esc, expected):
        assert _decode_partial("a" + BS + esc + "b" + Q, 0)[0] == "a" + expected + "b"

    def test_an_unknown_escape_keeps_the_letter(self):
        assert _decode_partial("a" + BS + "zb" + Q, 0)[0] == "azb"

    def test_a_valid_unicode_escape_is_decoded(self):
        """Acotado en tiempo: el avance del indice tras un escape unicode (`i += 6`) vale para los validos Y los malos; si se rompe, el bucle no termina."""
        assert run_bounded("h" + u("00e9") + "!" + Q) == (True, ("h\u00e9!", 9, True))

    def test_unicode_hex_is_case_insensitive(self):
        upper, lower = run_bounded(u("00E9") + Q), run_bounded(u("00e9") + Q)

        assert upper == lower == (True, ("\u00e9", 7, True))

    def test_an_escaped_quote_does_not_close_the_string(self):
        text, _, closed = _decode_partial("di " + BS + Q + "hola" + BS + Q + " ok" + Q, 0)
        assert closed and text == "di " + Q + "hola" + Q + " ok"


class TestPartial:
    def test_an_unclosed_string_reports_not_closed_and_keeps_what_it_has(self):
        assert _decode_partial("abc", 0) == ("abc", 3, False)

    def test_a_trailing_backslash_waits_for_the_next_fragment(self):
        assert _decode_partial("a" + BS, 0) == ("a", 1, False)

    def test_a_cut_unicode_escape_waits_for_the_next_fragment(self):
        assert _decode_partial("x" + BS + "u00", 0) == ("x", 1, False)

    def test_resuming_after_a_cut_gives_the_full_text(self):
        """El texto acumulado CRECE (`LiveNarrator.raw += fragmento`): se reanuda sobre el mismo buffer, desde la posicion que devolvio la llamada anterior."""
        buffer = "x" + BS + "u00"
        first, pos, closed = _decode_partial(buffer, 0)
        assert (first, pos, closed) == ("x", 1, False)

        buffer += "e9y" + Q  # llega el resto del escape y el cierre
        done, out = run_bounded(buffer, pos)

        assert done and out is not None and out[2] is True and first + out[0] == "x\u00e9y"

    def test_an_empty_input_is_an_empty_unclosed_string(self):
        assert _decode_partial("", 0) == ("", 0, False)


class TestAlwaysTerminates:
    """`_decode_partial` corre por CADA fragmento que llega del modelo, dentro del hilo del turno: un bucle infinito congelaria la narracion y el turno entero.
    Se ejecuta en un SUBPROCESO con limite de tiempo: un hilo de Python que gira en un bucle puro no se puede matar y retiene el GIL (se probo: el limite de
    2 s no se respetaba y la suite esperaba 65 s al timeout general). Un subproceso si se termina, y el test FALLA en segundos en vez de colgar la suite."""

    _finishes = staticmethod(run_bounded)

    def test_a_malformed_unicode_escape_does_not_loop_forever(self):
        done, out = self._finishes("x" + u("ZZZZ") + "y" + Q)

        assert done, "el decodificador se colgo con un escape unicode mal formado"
        assert out == ("xy", 9, True)

    def test_a_string_of_only_malformed_escapes_terminates(self):
        done, _ = self._finishes(u("QQQQ") * 200 + Q)

        assert done

    def test_every_call_makes_progress_through_the_input(self):
        """La posicion devuelta nunca retrocede ni se queda quieta cuando hay algo decodificable."""
        raw = "ab" + BS + "ncd" + Q
        _, pos, _ = _decode_partial(raw, 0)

        assert pos == len(raw)


class TestBestEffort:
    """Los tres casos corren acotados en tiempo (ver `run_bounded`): si el decodificador se cuelga con un escape malo, fallan en segundos."""

    def test_a_malformed_unicode_escape_is_skipped_and_decoding_continues(self):
        """Una secuencia de escape unicode cuyos 4 caracteres no son hexadecimales (barra, u, ZZZZ) se OMITE -- no se inventa un caracter -- y lo que sigue se decodifica igual."""
        done, out = run_bounded("x" + u("ZZZZ") + "y" + Q)

        assert done and out is not None and out[2] is True and out[0] == "xy"

    def test_a_malformed_escape_does_not_swallow_the_closing_quote(self):
        done, out = run_bounded(u("12G4") + Q + "resto")

        assert done and out == ("", 7, True)

    def test_a_valid_escape_right_after_a_malformed_one_still_decodes(self):
        done, out = run_bounded(u("ZZZZ") + u("0041") + Q)

        assert done and out is not None and out[0] == "A"
