"""Scannen, Gruppieren, MusicBrainz-Abgleich und Erstellen des Änderungsplans."""
import fnmatch
import hashlib
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor

from .mb import credit_id, credit_name
from .tags import open_tags
from .util import (AUDIO_EXTS, VARIOUS_ARTISTS, disc_from_dirname, image_pixels, majority, norm,
                   picture_problem,
                   parse_filename, primary_artist, safe_name, similarity, simplify_typo,
                   split_name, year_of)

DEFAULT_SETTINGS = {
    "root": "",
    "subdir": "",
    "use_mb": True,
    "year_mode": "original",        # original | release
    "simplify_typo": True,
    "quarantine": "_quarantaene",
    "cover_name": "Cover.jpg",      # großes Bild im Ordner (MusicBee sucht Cover.*)
    "fetch_cover": True,
    "embed_cover": True,
    "embed_max": 500,               # max. Kantenlänge eingebetteter Cover in px
    "allowed": "folder.jpg, folder.jpeg, folder.png, cover.jpg, cover.jpeg, cover.png, "
               "front.jpg, front.jpeg, front.png, back.jpg, back.jpeg, back.png, artist.jpg, artist.png",
    "allowed_dirs": "scans, artwork, covers, booklet",
    "allowed_root": "*.m3u, *.m3u8, *.pls, *.wpl, *.xspf",   # nur direkt im Wurzelordner
}

LABELS = {"title": "Titel", "artist": "Interpret", "album": "Album", "albumartist": "Albuminterpret",
          "track": "Track", "tracktotal": "Tracks gesamt", "disc": "CD", "disctotal": "CDs gesamt",
          "year": "Jahr", "genre": "Genre", "compilation": "Sampler", "mbids": "MusicBrainz-IDs"}

_GENERIC_TITLE = re.compile(r"^(track|titel|title|audiotrack|audio track|unknown|unbekannt|piste|spur|"
                            r"pista)?\s*[-_ ]?\s*\d*$", re.I)
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".pdf"}
COVER_PREF = ["folder", "cover", "front"]

UNKNOWN_ALBUM = "Unbekanntes Album"
UNKNOWN_ARTIST = "Unbekannter Interpret"
UNKNOWN_TITLE = "Unbekannter Titel"


def _id(s):
    return hashlib.md5(s.encode("utf-8", "surrogateescape")).hexdigest()[:12]


def _depth(rel_dir):
    return 0 if rel_dir in ("", ".") else rel_dir.count(os.sep) + 1


def _parts(s):
    return [p.strip().lower() for p in re.split(r"[,;\n]", s or "") if p.strip()]


# ===========================================================================
# 1) Scannen
# ===========================================================================
def _unreadable_reason(path):
    """Verständlicher Grund, warum eine Audiodatei nicht gelesen werden kann."""
    import mutagen
    try:
        f = mutagen.File(path)
    except Exception as e:
        msg = str(e)
        if "sync" in msg.lower() or "frame" in msg.lower():
            return f"Datei beschädigt – kein gültiger Audioanfang gefunden ({msg})"
        return f"Datei beschädigt oder nicht lesbar ({msg})"
    if f is None:
        return "Format nicht unterstützt"
    return "Tags nicht lesbar"


def scan(S, progress, should_stop):
    root = os.path.abspath(S["root"])
    base = os.path.abspath(os.path.join(root, S.get("subdir") or ""))
    if not os.path.isdir(base):
        raise RuntimeError(f"Ordner nicht gefunden: {base}")
    qdir = S["quarantine"].lower()
    audio_paths, others, empty_dirs = [], [], []
    progress("Dateien suchen", 0, 0, base, "Aktueller Ordner")
    for dirpath, dirnames, filenames in os.walk(base):
        if should_stop():
            raise InterruptedError()
        if os.path.abspath(dirpath) == root:
            dirnames[:] = [d for d in dirnames if d.lower() != qdir]
        dirnames[:] = sorted(d for d in dirnames if d != ".tag-corrector")
        rel_dir = os.path.relpath(dirpath, root)
        if not filenames and not dirnames and dirpath != root:
            empty_dirs.append(rel_dir)
        for fn in sorted(filenames):
            p = os.path.join(dirpath, fn)
            if split_name(fn)[1].lower() in AUDIO_EXTS and not fn.startswith("._"):
                audio_paths.append(p)
            else:
                others.append(p)
        progress("Dateien suchen", len(audio_paths), 0, rel_dir, "Aktueller Ordner")

    total = len(audio_paths)
    done = [0]
    done_lock = threading.Lock()

    def read(p):
        rel = os.path.relpath(p, root)
        name = os.path.basename(p)
        stem, ext = split_name(name)
        rec = {"id": _id(rel), "path": p, "rel": rel, "dir_rel": os.path.dirname(rel) or ".",
               "name": name, "stem": stem, "ext": ext.lower(), "size": os.path.getsize(p),
               "error": None, "companions": []}
        tf = open_tags(p)
        if tf is None:
            rec.update({"tags": {}, "junk": [], "has_cover": False, "pics": [], "length": 0, "bitrate": 0,
                        "lossless": False, "kind": "?", "writable": False, "error": _unreadable_reason(p)})
        else:
            try:
                rec.update(tf.read())
            except Exception as e:  # defekte Datei
                rec.update({"tags": {}, "junk": [], "has_cover": False, "pics": [], "length": 0, "bitrate": 0,
                            "lossless": False, "kind": tf.kind, "writable": False,
                            "error": f"Fehler beim Lesen: {e}"})
        with done_lock:
            done[0] += 1
            n = done[0]
        progress("Tags lesen", n, total, rel, "Aktuelles Lied")
        return rec

    progress("Tags lesen", 0, total, "", "Aktuelles Lied")
    with ThreadPoolExecutor(max_workers=8) as ex:
        files = []
        for rec in ex.map(read, audio_paths):
            if should_stop():
                raise InterruptedError()
            files.append(rec)

    # Begleitdateien (.lrc mit gleichem Namen) an das Lied hängen
    by_stem = {(f["dir_rel"], f["stem"].lower()): f for f in files}
    extras = []
    for p in others:
        rel = os.path.relpath(p, root)
        d = os.path.dirname(rel) or "."
        stem, ext = os.path.splitext(os.path.basename(p))
        if ext.lower() == ".lrc" and (d, stem.lower()) in by_stem:
            by_stem[(d, stem.lower())]["companions"].append(rel)
            continue
        e = {"rel": rel, "dir_rel": d, "name": os.path.basename(p), "allowed": _is_allowed(rel, S)}
        if stem.lower() in COVER_PREF and ext.lower() in IMAGE_EXTS:
            try:
                e["size"] = os.path.getsize(p)
            except OSError:
                e["size"] = 0
            e["pixels"] = image_pixels(p)
        extras.append(e)
    return {"root": root, "base": base, "files": files, "extras": extras, "empty_dirs": empty_dirs}


def _is_allowed(rel, S):
    name = os.path.basename(rel).lower()
    if not os.path.dirname(rel) and any(fnmatch.fnmatch(name, pat) for pat in _parts(S.get("allowed_root"))):
        return True
    if any(fnmatch.fnmatch(name, pat) for pat in _parts(S["allowed"])):
        return True
    parent = os.path.basename(os.path.dirname(rel)).lower()
    return parent in _parts(S["allowed_dirs"]) and os.path.splitext(name)[1] in IMAGE_EXTS


# ===========================================================================
# 2) Gruppieren zu Alben
# ===========================================================================
def _prepare_file(f):
    t = f["tags"]
    fdisc, fnum, ftitle = parse_filename(f["stem"])
    f["_fn_title"] = ftitle
    titles = []
    if t.get("title") and not _GENERIC_TITLE.match(t["title"]):
        titles.append(t["title"])
    if ftitle and not _GENERIC_TITLE.match(ftitle):
        titles.append(ftitle)
        if " - " in ftitle:
            titles.append(ftitle.rsplit(" - ", 1)[1])
    f["_titles"] = titles
    f["_num"] = t.get("track") or fnum
    f["_disc"] = t.get("disc") or f.get("dir_disc") or fdisc


def group_files(lib):
    by_dir = {}
    for f in lib["files"]:
        d = f["dir_rel"]
        disc = disc_from_dirname(os.path.basename(d)) if d != "." else None
        gdir = (os.path.dirname(d) or ".") if disc else d
        f["dir_disc"] = disc
        _prepare_file(f)
        by_dir.setdefault(gdir, []).append(f)

    groups = []
    for gdir, fl in sorted(by_dir.items()):
        depth = _depth(gdir)
        sub = {}
        for f in fl:
            sub.setdefault(norm(f["tags"].get("album")), []).append(f)
        if depth >= 2 and len(sub) > 1:
            # Albumordner mit uneinheitlichen Album-Tags -> ähnliche zusammenfassen
            keys = sorted(sub, key=lambda k: -len(sub[k]))
            main = keys[0] if keys[0] else (keys[1] if len(keys) > 1 else "")
            merged = {main: list(sub[main])}
            for k in keys:
                if k == main:
                    continue
                if not k or similarity(k, main) >= 0.55 or similarity(k, os.path.basename(gdir)) >= 0.55 \
                        or len(sub[k]) <= max(1, len(fl) // 5):
                    merged[main] += sub[k]
                else:
                    merged[k] = sub[k]
            sub = merged
        elif "" in sub and len(sub) == 2 and depth >= 1:
            other = next(k for k in sub if k)
            sub = {other: sub[other] + sub[""]}
        for key, files in sub.items():
            groups.append({"id": _id(gdir + "|" + key), "dir_rel": gdir, "depth": depth,
                           "album_key": key, "files": files})

    # Lose Dateien (Wurzel-/Interpretenebene) dem passenden Albumordner zuschlagen
    def artist_key(g):
        return norm(majority([primary_artist(f["tags"].get("albumartist") or f["tags"].get("artist"))
                              for f in g["files"]]))
    albums = {}
    for g in groups:
        if g["depth"] >= 2 and g["album_key"]:
            albums.setdefault((g["album_key"], artist_key(g)), g)
    merged = []
    for g in groups:
        target = albums.get((g["album_key"], artist_key(g))) if g["depth"] < 2 and g["album_key"] else None
        if target is not None:
            target["files"] += g["files"]
        else:
            merged.append(g)
    for g in merged:
        g["files"].sort(key=lambda f: ((f["_disc"] or 1), f["_num"] or 999, f["name"].lower()))
    return merged


def _hints(g):
    files = g["files"]
    tag_album = majority([f["tags"].get("album") for f in files])
    tag_aa = majority([f["tags"].get("albumartist") for f in files], 0.5)
    tag_artist = majority([primary_artist(f["tags"].get("artist")) for f in files], 0.5)
    parts = [] if g["dir_rel"] == "." else g["dir_rel"].split(os.sep)
    folder_album = parts[-1] if g["depth"] >= 2 else None
    folder_artist = parts[-2] if g["depth"] >= 2 else (parts[0] if g["depth"] == 1 else None)
    return {"tag_album": tag_album, "tag_artist": tag_aa or tag_artist,
            "folder_album": folder_album, "folder_artist": folder_artist}


# ===========================================================================
# 3) MusicBrainz-Abgleich
# ===========================================================================
def _release_summary(r, pre=None):
    media = r.get("media") or []
    return {"id": r["id"], "title": r.get("title", ""), "artist": credit_name(r.get("artist-credit")),
            "date": r.get("date", ""), "country": r.get("country", ""),
            "tracks": r.get("track-count") or sum((m.get("track-count") or 0) for m in media),
            "formats": " + ".join(sorted({m.get("format") or "?" for m in media})) if media else "",
            "status": r.get("status", ""), "score": round(pre, 2) if pre is not None else None}


def _prescore(r, album, artist, n, year):
    s_al = similarity(r.get("title"), album)
    s_ar = similarity(credit_name(r.get("artist-credit")), artist) if artist else 0.5
    tc = r.get("track-count") or sum((m.get("track-count") or 0) for m in r.get("media") or [])
    if tc >= n:   # unvollständige Alben sind häufig -> milde Strafe
        s_tc = 1.0 - 0.5 * min(1.0, (tc - n) / max(n, 4))
    else:
        s_tc = max(0.0, 1 - (n - tc) / max(n, 4))
    s = 0.4 * s_al + 0.3 * s_ar + 0.25 * s_tc + 0.05 * (r.get("score", 0) / 100)
    rtype = (r.get("release-group") or {}).get("primary-type")
    if rtype == "Album":
        s += 0.05
    elif rtype in ("Single", "EP") and tc < n:
        s -= 0.1
    if year and (r.get("date") or "")[:4] == year:
        s += 0.03
    if r.get("status") == "Official":
        s += 0.02
    return s


def _mb_tracks(rel):
    out = []
    for m in rel.get("media") or []:
        disc = m.get("position") or 1
        for t in m.get("tracks") or []:
            rec = t.get("recording") or {}
            ac = t.get("artist-credit") or rec.get("artist-credit")
            out.append({"disc": disc, "num": t.get("position"), "title": t.get("title") or rec.get("title", ""),
                        "artist": credit_name(ac), "artistid": credit_id(ac),
                        "length": (t.get("length") or rec.get("length") or 0) / 1000,
                        "rec": rec.get("id"), "total": m.get("track-count") or len(m.get("tracks") or [])})
    return out


_PAREN = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
MIN_TITLE_SIM = 0.5   # darunter wird ein Lied nie zugeordnet, auch wenn Länge/Nummer passen


# Klammern mit diesen Wörtern kennzeichnen eine andere Aufnahme und werden nie ignoriert
_VERSION_WORDS = re.compile(r"live|remix|rmx|mix\b|rework|dub\b|instrumental|acoustic|akusti|unplugged|demo|"
                            r"edit\b|version|karaoke|reprise|session|bootleg|cover", re.I)


def _strip_parens(title):
    return _PAREN.sub(lambda m: m.group(0) if _VERSION_WORDS.search(m.group(0)) else "", title or "").strip()


def _title_sim(file_title, mb_title):
    """Ähnlichkeit Datei-Titel zu MusicBrainz-Titel. Neutrale Klammerzusätze im Datei-Titel
    („(Episode 2)“) werden zusätzlich ignoriert – im MusicBrainz-Titel nie, denn dort
    unterscheiden sie oft Aufnahmen („(Earls Court 10-15-94)“)."""
    s = similarity(file_title, mb_title)
    stripped = _strip_parens(file_title)
    if stripped and stripped != file_title:
        s = max(s, similarity(stripped, mb_title))
    return s


def _is_version(f):
    """Datei als besondere Fassung gekennzeichnet (Live, Remix, Instrumental …)?"""
    return any(_VERSION_WORDS.search(m.group(0)) for x in f["_titles"] for m in _PAREN.finditer(x))


def _best_title_sim(f, t):
    return max(_title_sim(x, t["title"]) for x in f["_titles"]) if f["_titles"] else None


def _track_score(f, t):
    parts = []
    if f["_titles"]:
        parts.append((0.55, _best_title_sim(f, t)))
    if f.get("length") and t["length"]:
        parts.append((0.3, max(0.0, 1 - abs(f["length"] - t["length"]) / 15)))
    if not parts:
        return 0.0
    if f["_num"]:
        hit = f["_num"] == t["num"]
        parts.append((0.15, 1.0 if hit and (f["_disc"] or 1) == t["disc"] else (0.4 if hit else 0.0)))
    return sum(w * v for w, v in parts) / sum(w for w, _ in parts)


def match_tracks(rel, files):
    mbt = _mb_tracks(rel)
    pairs = []
    for fi, f in enumerate(files):
        for ti, t in enumerate(mbt):
            s = _track_score(f, t)
            ts = _best_title_sim(f, t)
            if s >= 0.45 and (ts is None or ts >= MIN_TITLE_SIM):
                pairs.append((s, fi, ti))
    pairs.sort(key=lambda x: -x[0])
    assign, used = {}, set()
    for s, fi, ti in pairs:
        if fi in assign or ti in used:
            continue
        assign[fi] = (ti, s)
        used.add(ti)
    # Übrige Dateien, die eindeutig einem bereits belegten Track entsprechen -> Duplikat desselben Liedes.
    # Nie, wenn nur eine der beiden Dateien als Live/Remix/... gekennzeichnet ist (andere Aufnahme).
    holder = {ti: fi for fi, (ti, _) in assign.items()}
    for s, fi, ti in pairs:
        if fi not in assign and s >= 0.75 and _is_version(files[fi]) == _is_version(files[holder[ti]]):
            assign[fi] = (ti, s)
    return mbt, assign


def match_group(g, mb, S, forced_mbid=None, query=None):
    files = g["files"]
    n = len(files)
    h = g.setdefault("hints", _hints(g))
    year = majority([f["tags"].get("year") for f in files])
    candidates = {}
    from_tags = False
    if forced_mbid:
        try_ids = [forced_mbid]
    else:
        tag_mbid = majority([f["tags"].get("mb_albumid") for f in files], 0.5)
        if tag_mbid and not query:
            try_ids, from_tags = [tag_mbid], True
        else:
            pairs = []
            if query:
                pairs.append((query.get("album"), query.get("artist") or None))
            else:
                for alb, art in ((h["tag_album"], h["tag_artist"]), (h["folder_album"], h["folder_artist"]),
                                 (h["tag_album"] or h["folder_album"], None)):
                    if alb and norm(alb) and (norm(alb), norm(art)) not in [(norm(a), norm(b)) for a, b in pairs]:
                        pairs.append((alb, art))
            for alb, art in pairs[:3]:
                if not alb:
                    continue
                for r in mb.search_releases(alb, art):
                    pre = _prescore(r, alb, art, n, year)
                    if r["id"] not in candidates or candidates[r["id"]][0] < pre:
                        candidates[r["id"]] = (pre, r)
                if candidates and max(c[0] for c in candidates.values()) >= 0.85:
                    break
            ranked = sorted(candidates.values(), key=lambda c: -c[0])
            try_ids = [r["id"] for pre, r in ranked[:5] if pre >= 0.45]

    album_hints = [x for x in (h["tag_album"], h["folder_album"], query and query.get("album")) if x]
    best = None
    for rid in try_ids:
        rel = mb.release(rid)
        if not rel or not rel.get("media"):
            continue
        mbt, assign = match_tracks(rel, files)
        avg = sum(s for _, s in assign.values()) / max(n, 1)
        alb_sim = 1.0 if (from_tags or forced_mbid) else \
            (max(similarity(rel["title"], a) for a in album_hints) if album_hints else 0.5)
        conf = 0.8 * avg + 0.2 * alb_sim
        rank = conf - 0.01 * min(abs(len(mbt) - n), 10)
        if best is None or rank > best["rank"]:
            best = {"release": rel, "mbt": mbt, "assign": assign, "conf": round(conf, 3), "rank": rank}
        if conf >= 0.92 and len(mbt) == n:
            break
        if best["conf"] >= 0.85 and rid != try_ids[0]:
            break
    cand_list = [_release_summary(r, pre) for pre, r in sorted(candidates.values(), key=lambda c: -c[0])[:12]]
    if best is None:
        return {"release": None, "conf": 0, "candidates": cand_list, "forced": bool(forced_mbid)}
    best["candidates"] = cand_list
    best["forced"] = bool(forced_mbid)
    return best


# ===========================================================================
# 4) Plan pro Album
# ===========================================================================
def _track_name(new, ext, multi, stem):
    title = safe_name(new.get("title") or stem)
    if new.get("track"):
        num = f"{int(new['track']):02d}"
        if multi and new.get("disc"):
            num = f"{new['disc']}-{num}"
        return f"{num} - {title}{ext}"
    return f"{title}{ext}"


def _strip_artist_prefix(title, artist):
    if title and artist and " - " in title:
        a, b = title.split(" - ", 1)
        if similarity(a, artist) > 0.8:
            return b
    return title


def _offline_albumartist(files, folder_artist):
    aa = majority([f["tags"].get("albumartist") for f in files], 0.5)
    if aa:
        return aa
    arts = [primary_artist(f["tags"].get("artist")) for f in files]
    main = majority(arts, 0.5)
    if main:
        return main
    if len({norm(a) for a in arts if a}) > 1:
        return VARIOUS_ARTISTS
    return majority(arts) or folder_artist or UNKNOWN_ARTIST


_ARTIST_JOIN = r"\s*(?:und|and|&|\+|,|/|-|–|x|vs\.?|feat\.?|ft\.?|featuring|with|mit)\s"


def _keep_folder_artist(aa, folder_artist):
    """„Helge Schneider und meine kleine Band“ im Ordner „Helge Schneider“ -> Ordner bleibt."""
    fa = (folder_artist or "").strip()
    if not fa or not aa or norm(fa) == norm(aa) or norm(fa) == norm(VARIOUS_ARTISTS):
        return aa
    if re.match(re.escape(fa) + _ARTIST_JOIN, aa, re.I):
        return fa
    return aa


def _fmt(v):
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return "ja" if v else "nein"
    return str(v)


def plan_album(g, match, S, ov=None):
    ov = ov or {}
    files = g["files"]
    embed_max = int(S.get("embed_max") or 500)
    h = g.setdefault("hints", _hints(g))
    tr = simplify_typo if S["simplify_typo"] else (lambda x: x)
    rel = match.get("release") if match else None
    conf = match.get("conf", 0) if match else 0
    status, use_rel = "offline", False
    if rel and S["use_mb"] and not ov.get("offline"):
        if conf >= 0.8 or match.get("forced"):
            status, use_rel = "mb_ok", True
        elif conf >= 0.6:
            status, use_rel = "mb_unsure", True
        else:
            status = "nomatch"
    elif S["use_mb"] and not ov.get("offline"):
        status = "nomatch"
    assign = match.get("assign", {}) if use_rel else {}
    mbt = match.get("mbt", []) if use_rel else []
    warnings, info = [], []

    # ---- Album-Ebene -----------------------------------------------------
    tag_years = [f["tags"].get("year") for f in files]
    if use_rel:
        album = rel["title"]
        aa = credit_name(rel.get("artist-credit"))
        aa_id = credit_id(rel.get("artist-credit"))
        rg = rel.get("release-group") or {}
        y_orig, y_rel = year_of(rg.get("first-release-date")), year_of(rel.get("date"))
        year = (y_orig or y_rel) if S["year_mode"] == "original" else (y_rel or y_orig)
        year = year or majority(tag_years)
        disctotal = len(rel.get("media") or []) or None
        genres = sorted(rel.get("genres") or [], key=lambda x: -x.get("count", 0))
        mb_genre = genres[0]["name"].title() if genres else None
    else:
        album = h["tag_album"] or h["folder_album"]
        aa = _offline_albumartist(files, h["folder_artist"])
        aa_id = None
        year = majority(tag_years)
        discs = [f["_disc"] for f in files if f["_disc"]]
        disctotal = max(discs) if discs and max(discs) > 1 else majority([f["tags"].get("disctotal") for f in files])
        mb_genre = None
        if not album:
            album = UNKNOWN_ALBUM
            warnings.append("Kein Albumname ermittelbar – bitte manuell setzen")
            status = "unknown"

    # Track-Interpreten vorab bestimmen (für Hauptinterpret-Regel)
    track_info = []
    for fi, f in enumerate(files):
        t = f["tags"]
        if fi in assign:
            mt = mbt[assign[fi][0]]
            ti = {"title": mt["title"], "artist": mt["artist"], "track": mt["num"], "tracktotal": mt["total"],
                  "disc": mt["disc"], "mb_trackid": mt["rec"], "mb_artistid": mt["artistid"],
                  "score": round(assign[fi][1], 2), "mb_title": mt["title"]}
        else:
            artist = t.get("artist") or t.get("albumartist") or (aa if aa != VARIOUS_ARTISTS else None) \
                or h["folder_artist"] or UNKNOWN_ARTIST
            title = t.get("title") or _strip_artist_prefix(f["_fn_title"], artist) or f["stem"]
            if not title:
                title = UNKNOWN_TITLE
                f_warn = "Kein Titel ermittelbar – bitte prüfen"
            else:
                f_warn = None
            ti = {"title": title, "artist": artist, "track": f["_num"], "tracktotal": t.get("tracktotal"),
                  "disc": f["_disc"], "score": None}
            if use_rel:
                ti["warn"] = "Lied wurde im MusicBrainz-Release nicht gefunden (Bonustrack?) – Tags aus Datei"
            if f_warn:
                ti["warn"] = ((ti.get("warn") + ". ") if ti.get("warn") else "") + f_warn
        track_info.append(ti)

    if norm(aa) == norm(VARIOUS_ARTISTS) or not aa:
        main = majority([primary_artist(ti["artist"]) for ti in track_info], 0.5001)
        aa = main or VARIOUS_ARTISTS
        aa_id = None if main else "89ad4ac3-39f7-470e-963a-56509c546377"
    compilation = norm(aa) == norm(VARIOUS_ARTISTS)

    # Manuelle Überschreibungen
    if ov.get("albumartist"):
        aa, aa_id = ov["albumartist"], None
    if ov.get("album"):
        album = ov["album"]
    if ov.get("year"):
        year = ov["year"]
    if ov:
        if status == "unknown" and ov.get("album"):
            status = "offline"
        if ov.get("albumartist") or ov.get("album") or ov.get("year"):
            status = "manual" if not use_rel else status

    album, aa = tr(album), tr(aa)
    multi = bool(disctotal and disctotal > 1)
    dir_artist = _keep_folder_artist(aa, h["folder_artist"]) if not ov.get("albumartist") else aa
    if dir_artist != aa:
        info.append(f"Bleibt im vorhandenen Interpreten-Ordner „{dir_artist}“ (Albuminterpret: {aa})")
    target_dir = os.path.join(safe_name(dir_artist), safe_name(album))
    album_genre = majority([f["tags"].get("genre") for f in files]) or mb_genre
    if not year:
        warnings.append("Jahr unbekannt – kann nicht gesetzt werden")

    # Tag-Inkonsistenzen dokumentieren
    n_albums = len({norm(f["tags"].get("album")) for f in files if f["tags"].get("album")})
    if n_albums > 1:
        info.append(f"{n_albums} verschiedene Albumnamen in den Tags gefunden")
    n_years = len({y for y in tag_years if y})
    if n_years > 1:
        info.append(f"{n_years} verschiedene Jahre in den Tags gefunden")
    if any(not y for y in tag_years):
        info.append(f"{sum(1 for y in tag_years if not y)} Lieder ohne Jahr")

    # Cover-Quelle
    src_dirs = sorted({f["dir_rel"] for f in files})
    album_extras = g.get("extras", [])
    images = [e for e in album_extras if e["allowed"] and os.path.splitext(e["name"])[1].lower() in IMAGE_EXTS
              and e["dir_rel"] in src_dirs + [g["dir_rel"]]]
    # Größtes Bild gewinnt (Pixel, dann Dateigröße); bei Gleichstand folder > cover > front
    candidates = [e for e in images if os.path.splitext(e["name"])[0].lower() in COVER_PREF]
    best = max(candidates, default=None,
               key=lambda e: (e.get("pixels", 0), e.get("size", 0),
                              -COVER_PREF.index(os.path.splitext(e["name"])[0].lower())))
    cover_src = best["rel"] if best else None

    items = []
    caa_front = bool(use_rel and (rel.get("cover-art-archive") or {}).get("front"))
    if not cover_src and use_rel and S["fetch_cover"] and caa_front:
        items.append({"id": "cover:" + g["id"], "kind": "cover", "mbid": rel["id"],
                      "dst": os.path.join(target_dir, S["cover_name"]),
                      "label": f"Cover von MusicBrainz laden → {S['cover_name']}"})
        cover_src = "download"
    elif not cover_src:
        info.append("Kein Cover vorhanden")

    base_dir = g["dir_rel"]
    for e in album_extras:
        if e["allowed"]:
            d = e["dir_rel"]
            if d == base_dir or disc_from_dirname(os.path.basename(d)):
                sub = e["name"]
            else:  # z.B. Scans/front.jpg
                sub = os.path.relpath(e["rel"], base_dir)
            dst = os.path.join(target_dir, sub)
            if dst != e["rel"]:
                items.append({"id": "carry:" + _id(e["rel"]), "kind": "carry", "src": e["rel"], "dst": dst,
                              "label": f"{e['name']} mitverschieben"})
        else:
            items.append({"id": "q:" + _id(e["rel"]), "kind": "quarantine", "src": e["rel"],
                          "dst": os.path.join(S["quarantine"], e["rel"]),
                          "label": f"Fremddatei {e['name']} → Quarantäne"})

    # ---- Track-Ebene -----------------------------------------------------
    tracks = []
    for fi, f in enumerate(files):
        t, ti = f["tags"], track_info[fi]
        new = {"title": tr(ti["title"]), "artist": tr(ti["artist"]), "album": album, "albumartist": aa,
               "track": ti["track"], "tracktotal": ti["tracktotal"], "disc": ti["disc"],
               "disctotal": disctotal if ti["disc"] else None, "year": year,
               "genre": t.get("genre") or album_genre, "compilation": compilation}
        if use_rel:
            new.update({"mb_trackid": ti.get("mb_trackid"), "mb_albumid": rel["id"],
                        "mb_releasegroupid": (rel.get("release-group") or {}).get("id"),
                        "mb_artistid": ti.get("mb_artistid"), "mb_albumartistid": aa_id})
        changes, write = [], {}
        for k in ("title", "artist", "album", "albumartist", "track", "tracktotal", "disc", "disctotal",
                  "year", "genre", "compilation"):
            old, nv = t.get(k), new[k]
            if k == "compilation":
                if bool(old) == bool(nv):
                    continue
            elif nv in (None, "") or _fmt(old) == _fmt(nv):
                continue
            changes.append({"field": k, "label": LABELS[k], "old": _fmt(old), "new": _fmt(nv)})
            write[k] = nv
        if "track" in write or "tracktotal" in write:
            write["track"], write["tracktotal"] = new["track"], new["tracktotal"]
        if "disc" in write or "disctotal" in write:
            write["disc"], write["disctotal"] = new["disc"], new["disctotal"]
        mb_changed = [k for k in ("mb_trackid", "mb_albumid", "mb_releasegroupid", "mb_artistid", "mb_albumartistid")
                      if new.get(k) and new.get(k) != t.get(k)]
        if mb_changed:
            for k in mb_changed:
                write[k] = new[k]
            changes.append({"field": "mbids", "label": LABELS["mbids"], "old": None, "new": "werden gesetzt",
                            "minor": True})
        if not f["writable"]:
            changes, write = [], {}
        name = _track_name(new, f["ext"], multi, f["stem"])
        fix_cover = []
        if S["embed_cover"] and f["writable"]:
            fix_cover = sorted({pr for pr in (picture_problem(pi, pi["mime"], embed_max) for pi in f.get("pics", []))
                                if pr})
        dst = os.path.join(target_dir, name)
        tw = list(filter(None, [ti.get("warn"), f.get("error")]))
        if ti.get("score") is not None and ti["score"] < 0.55:
            tw.append(f"Zuordnung zu „{ti['mb_title']}“ unsicher ({round(ti['score'] * 100)} %) – bitte prüfen")
        if not new["track"]:
            tw.append("Keine Tracknummer ermittelbar")
        companions = [{"src": c, "dst": os.path.splitext(dst)[0] + os.path.splitext(c)[1]} for c in f["companions"]]
        tracks.append({
            "id": f["id"], "src": f["rel"], "dst_plan": dst, "dst": dst, "changes": changes, "write": write,
            "junk": f["junk"] if f["writable"] else [],
            "embed_cover": bool(S["embed_cover"] and cover_src and f["writable"] and not f["has_cover"]),
            "fix_cover": fix_cover,
            "score": ti.get("score"), "warnings": tw, "dup": None, "companions": companions,
            "quality": (1 if f["lossless"] else 0, f["bitrate"], f["length"]),
            "kind": f["kind"], "length": f["length"], "bitrate": f["bitrate"],
        })

    # Lücken
    if use_rel:
        used = {i for i, _ in assign.values()}
        missing = [f"{m['disc']}-{m['num']:02d} {m['title']}" if multi else f"{m['num']:02d} {m['title']}"
                   for i, m in enumerate(mbt) if i not in used]
        if missing:
            info.append("Fehlende Lieder laut MusicBrainz: " + ", ".join(missing[:15]) +
                        (f" … (+{len(missing) - 15})" if len(missing) > 15 else ""))
    else:
        for d in sorted({ti["disc"] or 1 for ti in track_info}):
            nums = sorted({ti["track"] for ti in track_info if ti["track"] and (ti["disc"] or 1) == d})
            if nums:
                gaps = [x for x in range(1, max(nums) + 1) if x not in nums]
                if gaps:
                    info.append(f"Lücken in der Nummerierung{' (CD ' + str(d) + ')' if multi else ''}: " +
                                ", ".join(map(str, gaps[:20])))
    by_num = {}
    for ti in track_info:
        if ti["track"]:
            by_num.setdefault((ti["disc"] or 1, int(ti["track"])), set()).add(norm(ti["title"]))
    clashes = sorted(k for k, titles in by_num.items() if len(titles) > 1)   # gleiche Nummer, verschiedene Lieder
    if clashes:
        nums_txt = ", ".join((f"{d}-{n:02d}" if multi else f"{n:02d}") for d, n in clashes[:10])
        warnings.append(f"Doppelte Tracknummern ({nums_txt}) – Zuordnung bitte prüfen")
        if status == "mb_ok" and not (match or {}).get("forced"):
            status = "mb_unsure"

    album_plan = {
        "id": g["id"], "status": status, "confidence": conf,
        "release": _release_summary(rel) if rel else None,
        "release_group_id": (rel.get("release-group") or {}).get("id") if rel else None,
        "candidates": match.get("candidates", []) if match else [],
        "target": {"albumartist": aa, "album": album, "year": year, "disctotal": disctotal,
                   "compilation": compilation},
        "dir": target_dir, "source_dirs": src_dirs, "tracks": tracks, "items": items,
        "cover_src": cover_src, "embed_max": embed_max, "warnings": warnings, "info": info, "overrides": ov,
        "hints": h,
    }
    _summarize(album_plan, S)
    return album_plan


def _summarize(a, S=None):
    flags = set()
    n = 0
    for t in a["tracks"]:
        cats = []
        if t["dup"]:
            cats.append("duplikat")
        elif t["dst"] != t["src"]:
            cats.append("struktur")
        real = [x for x in t["changes"] if not x.get("minor")]
        if real:
            cats.append("tags")
            if any(x["field"] == "year" for x in real):
                cats.append("jahr")
        if any(j.get("comment") for j in t["junk"]):
            cats.append("kommentare")
        if any(not j.get("comment") for j in t["junk"]):
            cats.append("tagmuell")
        if t["embed_cover"] or t.get("fix_cover"):
            cats.append("cover")
        t["cats"] = cats
        t["has_changes"] = bool(cats or t["changes"])
        flags.update(cats)
        if t["has_changes"]:
            n += 1
    for it in a["items"]:
        flags.add({"quarantine": "fremddateien", "cover": "cover", "carry": "struktur"}[it["kind"]])
    if any("Fehlende" in i or "Lücken" in i for i in a["info"]):
        flags.add("luecken")
    a["flags"] = sorted(flags)
    a["n_changed_tracks"] = n
    a["has_changes"] = bool(n or a["items"])


def resolve_collisions(albums, S):
    """Mehrere Dateien mit gleichem Ziel -> beste behalten, Rest als Duplikat in Quarantäne."""
    by_target = {}
    for a in albums.values():
        for t in a["tracks"]:
            t["dst"], t["dup"] = t["dst_plan"], None
            by_target.setdefault(os.path.splitext(t["dst_plan"])[0].lower(), []).append((a, t))
    for lst in by_target.values():
        if len(lst) < 2:
            continue
        lst.sort(key=lambda at: at[1]["quality"], reverse=True)
        best = lst[0][1]
        for a, t in lst[1:]:
            t["dup"] = best["src"]
            t["dst"] = os.path.join(S["quarantine"], "_duplikate", t["src"])
    for a in albums.values():
        _summarize(a, S)


def assign_extras(groups, lib):
    """Nicht-Audio-Dateien den Albumgruppen zuordnen (nur Album-Ebene, Tiefe >= 2).
    Gibt die übrigen Dateien (Interpret-/Wurzelebene, Ordner ohne Musik) zurück."""
    by_dir = {}
    for e in lib["extras"]:
        by_dir.setdefault(e["dir_rel"], []).append(e)
    group_dirs = {}
    for g in groups:
        g["extras"] = []
        for f in g["files"]:
            if _depth(f["dir_rel"]) >= 2:
                group_dirs.setdefault(f["dir_rel"], g)
        if g["depth"] >= 2:
            group_dirs.setdefault(g["dir_rel"], g)
    for d in list(by_dir):
        g = group_dirs.get(d)
        if g is None and _depth(d) >= 3 and os.path.dirname(d) in group_dirs:
            g = group_dirs[os.path.dirname(d)]   # z.B. Album/Scans/
        if g is not None:
            g["extras"] += by_dir.pop(d)
    return by_dir


def misc_section(lib, extras_by_dir, S):
    """Fremddateien außerhalb von Albumordnern + leere Ordner."""
    items = []
    for d in sorted(extras_by_dir):
        for e in extras_by_dir[d]:
            if e["allowed"] and _depth(d) <= 1:
                continue  # z.B. artist.jpg im Interpreten-Ordner
            items.append({"id": "q:" + _id(e["rel"]), "kind": "quarantine", "src": e["rel"],
                          "dst": os.path.join(S["quarantine"], e["rel"]),
                          "label": f"Fremddatei {e['rel']} → Quarantäne"})
    for d in lib["empty_dirs"]:
        items.append({"id": "rmdir:" + _id(d), "kind": "rmdir", "src": d, "label": f"Leeren Ordner {d} entfernen"})
    return items
