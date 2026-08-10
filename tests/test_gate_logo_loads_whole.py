"""The sign-in logo must never appear as half a face.

Reported from a phone on two bars of signal: the top of the head and the arc
above it were drawn, the wordmark and tagline below were missing, and the rest of
the box was empty. It was not a layout bug — at a 393x852 viewport the box
measures a correct 160x190 with the aspect ratio intact. It was the file
arriving. A non-interlaced PNG paints top-down as it downloads, the artwork was
372 KB, and what was on screen was simply the rows that had turned up.

Two things follow, and this file holds both.

*Nothing partly downloaded is shown.* The image is hidden until the whole file
has arrived, and the box is reserved either way so revealing it moves nothing.
Marked by JavaScript rather than in the markup, so a browser that never runs it
shows the image as before instead of hiding it forever.

*The file has to be small enough that waiting for whole is short.* Hiding a
372 KB image only trades half a logo for a blank space of the same length. The
display copies are WebP, which was the only option measured that halved the bytes
without touching the alpha channel — and the alpha matters, because 36% of the
pixels are partially transparent so the card's surface shows through in either
theme. Palette-quantising the PNG was four times smaller and is not used: it
reached that size by destroying exactly that transparency.
"""
import pathlib
import re
import struct

FRONTEND = pathlib.Path(__file__).resolve().parent.parent / "frontend"
HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")
CSS = (FRONTEND / "css" / "styles.css").read_text(encoding="utf-8")
APP = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")


# ---------- nothing half-drawn is shown ----------

def test_the_gate_logo_is_hidden_until_it_is_whole():
    assert re.search(r"\.gate-logo\.pending\s*\{[^}]*opacity:\s*0", CSS), (
        "a partly-downloaded logo has to be invisible, not partly visible")


def test_javascript_marks_it_rather_than_the_markup():
    """If the script never runs, the image must be visible as before — not
    hidden forever by a class sitting in the HTML."""
    assert 'class="gate-logo pending"' not in HTML
    assert 'classList.add("pending")' in APP


def test_it_is_revealed_when_the_file_finishes():
    assert re.search(r'addEventListener\("load",\s*show\)', APP)


def test_a_failed_load_also_reveals_it():
    """Otherwise a load that starts and then fails leaves an invisible element
    where the fallback text should be."""
    assert re.search(r'addEventListener\("error",\s*show\)', APP)


def test_an_already_loaded_image_is_never_hidden():
    """Re-entering the gate with the file cached must not flash it away."""
    assert "img.complete && img.naturalWidth > 0" in APP


def test_the_box_is_reserved_so_revealing_it_moves_nothing():
    assert re.search(r"\.gate-logo\s*\{[^}]*height:\s*190px", CSS)


# ---------- and the wait is short ----------

def test_the_displayed_logo_is_the_webp():
    """Both display copies. The PNG stays for the favicon, where format support
    is least consistent and the file is fetched once."""
    assert 'src="/img/logo.webp"' in HTML
    assert HTML.count('src="/img/logo.webp"') == 2, "topbar mark and sign-in gate"
    assert 'rel="icon" type="image/png" href="/img/logo.png"' in HTML
    assert 'src="/img/logo.png"' not in HTML


def test_the_webp_exists_and_is_much_smaller_than_the_png():
    webp = FRONTEND / "img" / "logo.webp"
    png = FRONTEND / "img" / "logo.png"
    assert webp.exists(), "the markup points at it"
    assert webp.stat().st_size < png.stat().st_size * 0.6, (
        f"webp {webp.stat().st_size:,} vs png {png.stat().st_size:,} — the point "
        f"of the swap is that waiting for the whole file is quick")


def test_the_artwork_is_still_big_enough_for_a_dense_screen():
    """It renders 190px tall. Shrinking the source to save bytes would soften it
    on the phones that reported the problem."""
    png = (FRONTEND / "img" / "logo.png").read_bytes()
    w, h = struct.unpack(">II", png[16:24])
    assert (w, h) == (431, 512)
    assert h >= 190 * 2, "at least two device pixels per CSS pixel at 190px tall"


def test_the_png_still_has_its_soft_transparency():
    """The 4x-smaller palette version was rejected because it flattened this.
    A tRNS chunk or a palette colour type would mean it came back."""
    data = (FRONTEND / "img" / "logo.png").read_bytes()
    colour_type = data[25]
    assert colour_type == 6, (
        f"colour type {colour_type}: 6 is RGBA with a full 8-bit alpha channel; "
        f"3 would be a palette, which cannot express a soft edge")
