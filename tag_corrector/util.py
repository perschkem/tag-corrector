"""Hilfsfunktionen: Text-Normalisierung, Dateinamen, Ähnlichkeit."""
import io
import os
import re
import unicodedata
from difflib import SequenceMatcher

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".mp4", ".aac", ".ogg", ".oga", ".opus",
              ".wma", ".wav", ".aif", ".aiff", ".ape", ".wv", ".mpc"}

VARIOUS_ARTISTS = "Various Artists"

# Typografische Zeichen von MusicBrainz -> einfache ASCII-Varianten
_TYPO = str.maketrans({
    "\u2019": "'", "\u2018": "'", "\u201B": "'", "\u2032": "'",
    "\u201C": '"', "\u201D": '"', "\u201E": '"', "\u2033": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
    "\u2026": "...", "\u00A0": " ", "\u2009": " ", "\u200B": "",
})


def split_name(name):
    """Wie os.path.splitext, aber eine Datei, die nur aus der Endung besteht ('.mp3'),
    zählt als ('', '.mp3') statt als versteckte Datei ohne Endung."""
    stem, ext = os.path.splitext(name)
    if not ext and stem.startswith(".") and stem.count(".") == 1 and len(stem) > 1:
        return "", stem
    return stem, ext


def simplify_typo(s):
    return s.translate(_TYPO) if s else s


def norm(s):
    """Vergleichs-Normalform: klein, ohne Akzente/Satzzeichen/Klammerzusätze."""
    if not s:
        return ""
    s = simplify_typo(str(s)).lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("&", " and ")
    s = re.sub(r"\b(feat|ft|featuring)\b\.?.*$", "", s)
    s = re.sub(r"[\(\[][^\)\]]*(remaster|version|edit|mono|stereo|bonus|deluxe)[^\)\]]*[\)\]]", "", s)
    s = re.sub(r"[^\w]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def similarity(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def primary_artist(artist):
    """'A feat. B' / 'A & B' -> 'A' (für Hauptinterpret-Erkennung)."""
    if not artist:
        return ""
    a = re.split(r"\s+(?:feat\.?|ft\.?|featuring|with|vs\.?)\s+", artist, flags=re.I)[0]
    return a.strip()


_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(s, maxlen=120):
    """Dateisystem-sicherer Name (auch für Windows-Laufwerke unter WSL)."""
    s = simplify_typo(s or "").strip()
    s = s.replace("/", "-").replace("\\", "-").replace(":", " -").replace("?", "").replace("*", "")
    s = s.replace('"', "'").replace("<", "(").replace(">", ")").replace("|", "-")
    s = _ILLEGAL.sub("", s)
    s = re.sub(r"\s+", " ", s).strip().rstrip(". ")
    if s in ("", ".", ".."):
        s = "_"
    if len(s) > maxlen:
        s = s[:maxlen].rstrip(". ")
    return s


_LEADING_NUM = re.compile(r"^\s*(?:(\d{1,2})[-.](?=\d))?(\d{1,3})(?:\s*[-._)]\s*|\s+)(.*)$")


def parse_filename(stem):
    """'01 - Titel' / '1-03 Titel' / '03. Titel' -> (disc, track, title)."""
    m = _LEADING_NUM.match(stem)
    if m and m.group(3).strip():
        disc = int(m.group(1)) if m.group(1) else None
        title = m.group(3).strip()
        # "Interpret - Titel" Präfix entfernen, wenn vorhanden, wird später entschieden
        return disc, int(m.group(2)), title
    return None, None, stem.strip()


_DISC_DIR = re.compile(r"^(?:cd|disc|disk|scheibe)\s*[-_ ]?\s*(\d{1,2})\b", re.I)


def disc_from_dirname(name):
    m = _DISC_DIR.match(name.strip())
    return int(m.group(1)) if m else None


def to_int(v):
    if v is None:
        return None
    m = re.match(r"\s*(\d+)", str(v))
    return int(m.group(1)) if m else None


def year_of(v):
    if not v:
        return None
    m = re.search(r"(1[89]\d\d|20\d\d)", str(v))
    return m.group(1) if m else None


def windows_to_wsl(path):
    """'D:\\Musik' -> '/mnt/d/Musik' (praktisch unter WSL)."""
    p = path.strip().strip('"')
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", p)
    if m and os.name != "nt":
        return "/mnt/" + m.group(1).lower() + "/" + m.group(2).replace("\\", "/")
    return p


def wsl_to_windows(path):
    """'/mnt/e/Musik' -> 'E:\\Musik' (für Playlisten mit Windows-Pfaden), sonst None."""
    m = re.match(r"^/mnt/([a-zA-Z])(?:/(.*))?$", path.rstrip("/"))
    if not m:
        return None
    return m.group(1).upper() + ":\\" + (m.group(2) or "").replace("/", "\\")


def majority(values, min_share=0.0):
    """Häufigster Wert (ohne leere) oder None."""
    vals = [v for v in values if v]
    if not vals:
        return None
    counts = {}
    for v in vals:
        counts[v] = counts.get(v, 0) + 1
    best = max(counts.items(), key=lambda kv: kv[1])
    if best[1] / len(values) < min_share:
        return None
    return best[0]



# ---------------------------------------------------------------------------
# Bilder (Cover)
EMBED_MAX_BYTES = 250_000          # größere eingebettete Bilder werden neu komprimiert
_FMT_MIME = {"JPEG": "image/jpeg", "PNG": "image/png"}


def image_pixels(path):
    """Breite*Höhe einer Bilddatei (nur Header), 0 wenn unbekannt."""
    from PIL import Image
    try:
        with Image.open(path) as im:
            return im.width * im.height
    except Exception:
        return 0


def picture_info(data):
    """Kopfdaten eines eingebetteten Bildes, ohne es komplett zu dekodieren."""
    from PIL import Image
    info = {"bytes": len(data), "w": 0, "h": 0, "fmt": None}
    if data:
        try:
            with Image.open(io.BytesIO(data)) as im:
                info["w"], info["h"], info["fmt"] = im.width, im.height, im.format
        except Exception:
            pass
    return info


def picture_problem(info, mime, max_px):
    """Grund, warum ein eingebettetes Bild ersetzt werden sollte, sonst None.
    mime=None: Format kennt keinen MIME-Typ (MP4, APE)."""
    if info["bytes"] < 64:
        return "leer"
    fmt = info["fmt"]
    if fmt is None:
        return None                 # unbekanntes Format: lieber nicht anfassen
    if fmt not in _FMT_MIME:
        return fmt                  # GIF, BMP, ...
    if max(info["w"], info["h"]) > max_px or info["bytes"] > EMBED_MAX_BYTES:
        return "zu groß"
    if mime is not None and mime.lower() not in (_FMT_MIME[fmt], "image/jpg"):
        return "MIME-Typ"
    return None


def to_jpeg(data, max_px, quality=88):
    """Bild auf max_px (längste Seite) verkleinern, als Baseline-JPEG."""
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(data)) as im:
        im.draft("RGB", (max_px, max_px))
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P", "PA"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.getchannel("A"))
            im = bg
        else:
            im = im.convert("RGB")
        im.thumbnail((max_px, max_px), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, "JPEG", quality=quality, optimize=True)
        return out.getvalue()


def normalize_picture(data, mime, max_px):
    """None = Bild so lassen, b"" = entfernen, sonst (daten, mime) als Ersatz."""
    info = picture_info(data)
    problem = picture_problem(info, mime, max_px)
    if not problem:
        return None
    if problem == "leer":
        return b""
    if problem == "MIME-Typ":
        return data, _FMT_MIME[info["fmt"]]
    return to_jpeg(data, max_px), "image/jpeg"


def image_mime(data):
    return "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
