"""Ausführen des Plans (mit Undo-Log) und Rückgängig machen."""
import base64
import codecs
import hashlib
import json
import os
import re
import shutil
import time

from .mb import CACHE_DIR
from .tags import open_tags
from .util import AUDIO_EXTS, image_mime, normalize_picture, split_name, wsl_to_windows

UNDO_DIR = os.path.join(CACHE_DIR, "undo")
PIC_DIR = os.path.join(UNDO_DIR, "pictures")   # Originale ersetzter Cover, nach Inhalt abgelegt


def _save_pics(blob):
    os.makedirs(PIC_DIR, exist_ok=True)
    key = hashlib.sha1(blob).hexdigest()
    p = os.path.join(PIC_DIR, key + ".pkl")
    if not os.path.exists(p):
        with open(p + ".tmp", "wb") as fh:
            fh.write(blob)
        os.replace(p + ".tmp", p)
    return key


def _load_pics(key):
    with open(os.path.join(PIC_DIR, os.path.basename(key) + ".pkl"), "rb") as fh:
        return fh.read()


class Conflict(Exception):
    pass


class UndoLog:
    def __init__(self, root, summary):
        os.makedirs(UNDO_DIR, exist_ok=True)
        self.name = time.strftime("%Y%m%d-%H%M%S")
        self.path = os.path.join(UNDO_DIR, self.name + ".jsonl")
        self.fh = open(self.path, "a", encoding="utf-8")
        self.write({"op": "header", "root": root, "time": time.time(), "summary": summary})

    def write(self, entry):
        self.fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.fh.flush()
        os.fsync(self.fh.fileno())

    def close(self):
        self.fh.close()


def _mime(path):
    return "image/png" if path.lower().endswith(".png") else "image/jpeg"


class Executor:
    def __init__(self, root, log, progress, should_stop):
        self.root = root
        self.log = log
        self.progress = progress
        self.should_stop = should_stop
        self.errors = []
        self.done = {"tags": 0, "moved": 0, "quarantined": 0, "covers": 0, "rmdirs": 0, "touched": 0,
                     "playlists": 0}
        self.touched_dirs = set()
        self.moves = {}       # alter Pfad (klein) -> neuer Pfad, für Playlisten
        self.dir_moves = []   # (alt, neu) bei reiner Umbenennung von Ordnern
        self.dup_of = {}      # Duplikat (klein) -> Quelle der behaltenen Kopie

    def abs(self, rel):
        return os.path.join(self.root, rel)

    def err(self, what, e):
        self.errors.append(f"{what}: {e}")

    # ---- Grundoperationen -------------------------------------------------
    def ensure_dir(self, rel_dir):
        """Legt Zielordner an; korrigiert Groß/Kleinschreibung auf case-insensitiven Laufwerken."""
        cur = self.root
        for comp in [c for c in rel_dir.split(os.sep) if c and c != "."]:
            target = os.path.join(cur, comp)
            if os.path.isdir(cur) and comp not in os.listdir(cur):
                twin = next((e for e in os.listdir(cur) if e.lower() == comp.lower()), None)
                if twin and os.path.exists(target):  # gleiches Verzeichnis, nur andere Schreibweise
                    tmp = os.path.join(cur, twin + ".tc-tmp")
                    os.rename(os.path.join(cur, twin), tmp)
                    os.rename(tmp, target)
                    old_rel = os.path.relpath(os.path.join(cur, twin), self.root)
                    new_rel = os.path.relpath(target, self.root)
                    self.log.write({"op": "move", "src": old_rel, "dst": new_rel, "dir": True})
                    self.dir_moves.append((old_rel, new_rel))
            cur = target
        os.makedirs(cur, exist_ok=True)

    def move(self, src_rel, dst_rel, unique=False):
        src, dst = self.abs(src_rel), self.abs(dst_rel)
        if not os.path.exists(src):
            raise FileNotFoundError(src_rel)
        if os.path.exists(dst):
            if os.path.samefile(src, dst):
                if src == dst:
                    return dst_rel
                tmp = dst + ".tc-tmp"
                os.rename(src, tmp)
                os.rename(tmp, dst)
                self.log.write({"op": "move", "src": src_rel, "dst": dst_rel})
                self.moves[src_rel.lower()] = dst_rel
                return dst_rel
            if not unique:
                raise Conflict(f"Ziel existiert bereits: {dst_rel}")
            stem, ext = os.path.splitext(dst_rel)
            i = 2
            while os.path.exists(self.abs(f"{stem} ({i}){ext}")):
                i += 1
            dst_rel = f"{stem} ({i}){ext}"
            dst = self.abs(dst_rel)
        self.ensure_dir(os.path.dirname(dst_rel))
        shutil.move(src, dst)
        self.log.write({"op": "move", "src": src_rel, "dst": dst_rel})
        self.moves[src_rel.lower()] = dst_rel
        self.touched_dirs.add(os.path.dirname(src_rel))
        return dst_rel

    # ---- Ausführen ---------------------------------------------------------
    def run(self, albums, misc_items, sel, cats, mb):
        jobs = []
        for a in albums:
            s = sel.get(a["id"])
            if not s:
                continue
            tids, iids = set(s.get("tracks", [])), set(s.get("items", []))
            tracks = [t for t in a["tracks"] if t["id"] in tids]
            items = [i for i in a["items"] if i["id"] in iids]
            if tracks or items:
                jobs.append((a, tracks, items))
        total = sum(len(t) + len(i) for _, t, i in jobs) + len(misc_items) or 1
        n = 0
        deferred = []

        for a, tracks, items in jobs:
            if self.should_stop():
                self.errors.append("Abgebrochen durch Benutzer")
                break
            # Cover besorgen
            cover = None
            cover_item = next((i for i in items if i["kind"] == "cover"), None)
            need_embed = cats.get("cover") and any(t["embed_cover"] for t in tracks)
            if cats.get("cover") and (cover_item or need_embed):
                try:
                    if a["cover_src"] == "download":
                        mbid = (cover_item or {}).get("mbid") or (a.get("release") or {}).get("id")
                        data = mb.cover(mbid) if mbid else None
                        if data:
                            cover = (data, image_mime(data))
                    elif a["cover_src"]:
                        with open(self.abs(a["cover_src"]), "rb") as fh:
                            cover = (fh.read(), _mime(a["cover_src"]))
                except Exception as e:
                    self.err(f"Cover für {a['target']['album']}", e)
            if cover_item and cover:
                try:
                    dst = cover_item["dst"]
                    if cover[1] == "image/png" and dst.lower().endswith((".jpg", ".jpeg")):
                        dst = os.path.splitext(dst)[0] + ".png"
                    if not os.path.exists(self.abs(dst)):
                        self.ensure_dir(os.path.dirname(dst))
                        with open(self.abs(dst), "wb") as fh:
                            fh.write(cover[0])
                        self.log.write({"op": "create", "path": dst})
                        self.done["covers"] += 1
                except Exception as e:
                    self.err(f"Cover speichern {cover_item['dst']}", e)
            # Ordnerbild bleibt groß, eingebettet wird eine verkleinerte Fassung
            if cover:
                try:
                    small = normalize_picture(cover[0], cover[1], a.get("embed_max", 500))
                    cover = cover if small is None else (small or None)
                except Exception as e:
                    self.err(f"Cover verkleinern für {a['target']['album']}", e)
                    cover = None

            for t in tracks:
                self.progress("Änderungen schreiben", n, total, t["src"], "Aktuelles Lied")
                self._track(t, cats, cover, deferred, a.get("embed_max", 500))
                n += 1

            src_dirs = set(a["source_dirs"])
            for it in items:
                self.progress("Änderungen schreiben", n, total, it.get("src") or it.get("dst"), "Aktuelle Datei")
                self._item(it, cats, src_dirs)
                n += 1

        # Konflikte (z.B. Tausch zweier Dateien) erneut versuchen
        for _ in range(3):
            if not deferred:
                break
            retry, deferred = deferred, []
            for t, src, dst in retry:
                try:
                    self._move_track(t, src, dst)
                except Conflict:
                    deferred.append((t, src, dst))
                except Exception as e:
                    self.err(f"Verschieben {src}", e)
        for t, src, dst in deferred:
            self.err(f"Verschieben {src}", f"Ziel existiert bereits und ist keine Teil des Plans: {dst}")

        for it in misc_items:
            self.progress("Aufräumen", n, total, it.get("src"), "Aktuelle Datei")
            self._item(it, cats, set())
            n += 1

        if self.moves or self.dir_moves:
            self.progress("Aufräumen", n, total, "Playlisten anpassen", "Schritt")
            self.update_playlists()
        if cats.get("structure"):
            self.progress("Aufräumen", n, total, "leere Ordner entfernen", "Schritt")
            self.cleanup_empty_dirs()
        self.progress("Aufräumen", total, total, "fertig", "Schritt")
        return self.done, self.errors

    def _track(self, t, cats, cover, deferred, embed_max=500):
        src = t["src"]
        if not os.path.exists(self.abs(src)):
            self.err(src, "Datei nicht mehr vorhanden")
            return
        values = t["write"] if cats.get("tags") else {}
        junk = bool(cats.get("junk") and t["junk"])
        embed = cover if (cats.get("cover") and t["embed_cover"]) else None
        fix = embed_max if (cats.get("cover") and t.get("fix_cover")) else 0
        st = os.stat(self.abs(src))
        orig_times = (st.st_atime, st.st_mtime)
        tags_written = False
        if values or junk or embed or fix:
            try:
                tf = open_tags(self.abs(src))
                if tf is None:
                    raise RuntimeError("Datei nicht lesbar")
                snap = tf.snapshot()
                pics = _save_pics(tf.picture_state()) if fix else None
                tf.write(values, remove_junk=bool(cats.get("junk")), cover=embed, fix_pics=fix)
                entry = {"op": "tags", "path": src, "snap": snap, "cover_added": bool(embed), "times": orig_times}
                if pics:
                    entry["pics"] = pics
                self.log.write(entry)
                self.done["tags"] += 1
                tags_written = True
            except Exception as e:
                self.err(f"Tags schreiben {src}", e)
                return
        will_move = (cats.get("structure") and not t["dup"] and t["dst"] != src)
        # Änderungsdatum: auf jetzt setzen oder (Standard) das ursprüngliche behalten.
        # Umbenennen/Verschieben lässt das Datum ohnehin unverändert.
        try:
            if cats.get("touch") and (tags_written or will_move):
                os.utime(self.abs(src), None)
                self.log.write({"op": "utime", "path": src, "times": orig_times})
                self.done["touched"] += 1
            elif tags_written:
                os.utime(self.abs(src), orig_times)
        except OSError as e:
            self.err(f"Änderungsdatum {src}", e)
        if t["dup"]:
            if not cats.get("dups"):
                return
        elif not will_move:
            return
        try:
            self._move_track(t, src, t["dst"])
        except Conflict:
            deferred.append((t, src, t["dst"]))
        except Exception as e:
            self.err(f"Verschieben {src}", e)

    def _move_track(self, t, src, dst):
        final = self.move(src, dst, unique=bool(t["dup"]))
        if t["dup"]:
            self.dup_of[src.lower()] = t["dup"]
            self.done["quarantined"] += 1
        else:
            self.done["moved"] += 1
        for c in t.get("companions", []):
            try:
                cdst = os.path.splitext(final)[0] + os.path.splitext(c["src"])[1]
                self.move(c["src"], cdst, unique=True)
            except Exception as e:
                self.err(f"Begleitdatei {c['src']}", e)

    def _item(self, it, cats, src_dirs):
        kind = it["kind"]
        try:
            if kind == "quarantine" and cats.get("extras"):
                if os.path.exists(self.abs(it["src"])):
                    self.move(it["src"], it["dst"], unique=True)
                    self.done["quarantined"] += 1
            elif kind == "carry" and cats.get("structure"):
                d = os.path.dirname(it["src"])
                if not os.path.exists(self.abs(it["src"])):
                    return
                if self._has_audio(d):
                    return  # Ordner wird weiter benutzt -> Bild bleibt
                if os.path.exists(self.abs(it["dst"])) and not os.path.samefile(self.abs(it["src"]), self.abs(it["dst"])):
                    self.move(it["src"], os.path.join(cats.get("quarantine", "_quarantaene"), it["src"]), unique=True)
                else:
                    self.move(it["src"], it["dst"])
            elif kind == "rmdir" and cats.get("structure"):
                p = self.abs(it["src"])
                if os.path.isdir(p) and not os.listdir(p):
                    os.rmdir(p)
                    self.log.write({"op": "rmdir", "path": it["src"]})
                    self.done["rmdirs"] += 1
                    self.touched_dirs.add(os.path.dirname(it["src"]))
        except Exception as e:
            self.err(it["label"], e)

    def _has_audio(self, rel_dir):
        p = self.abs(rel_dir)
        if not os.path.isdir(p):
            return False
        for dp, _, fns in os.walk(p):
            if any(split_name(f)[1].lower() in AUDIO_EXTS for f in fns):
                return True
        return False

    # ---- Playlisten ---------------------------------------------------------
    def _resolve(self, rel):
        """Neuer Pfad einer verschobenen Datei (relativ zur Wurzel) oder None, wenn unverändert."""
        cur = os.path.normpath(rel)
        hit = False
        if cur.lower() in self.dup_of:   # aussortiertes Duplikat -> behaltene Kopie
            cur, hit = self.dup_of[cur.lower()], True
        nxt = self.moves.get(cur.lower())   # jede Datei wird pro Lauf nur einmal verschoben
        if nxt:
            cur, hit = nxt, True
        for old, new in self.dir_moves:
            if cur.lower().startswith(old.lower() + os.sep) and not cur.startswith(new + os.sep):
                cur, hit = new + cur[len(old):], True
        return cur if hit else None

    def _playlist_line(self, line, win_root):
        s = line.strip()
        if not s or s.startswith("#") or "://" in s:
            return line
        if re.match(r"^[A-Za-z]:[\\/]", s):          # E:\Media\Audio\Musik\...
            w = s.replace("/", "\\")
            if not win_root or not w.lower().startswith(win_root.lower() + "\\"):
                return line
            new = self._resolve(w[len(win_root) + 1:].replace("\\", os.sep))
            return win_root + "\\" + new.replace(os.sep, "\\") if new else line
        if s.startswith("/"):                          # /mnt/e/Media/Audio/Musik/...
            if not s.startswith(self.root + "/"):
                return line
            new = self._resolve(s[len(self.root) + 1:])
            return self.root + "/" + new if new else line
        back = "\\" in s                               # relativ zur Playlist (= Wurzel)
        new = self._resolve(s.replace("\\", "/"))
        if not new:
            return line
        return new.replace("/", "\\") if back else new

    def update_playlists(self):
        """.m3u/.m3u8 im Wurzelordner auf die neuen Dateinamen umschreiben (Original im Undo-Log)."""
        win_root = (wsl_to_windows(self.root) or "").rstrip("\\") or None
        try:
            names = sorted(os.listdir(self.root))
        except OSError:
            return
        for name in names:
            if not name.lower().endswith((".m3u", ".m3u8")) or not os.path.isfile(self.abs(name)):
                continue
            try:
                with open(self.abs(name), "rb") as fh:
                    raw = fh.read()
                try:
                    text, enc = raw.decode("utf-8-sig"), "utf-8"
                except UnicodeDecodeError:
                    text, enc = raw.decode("cp1252"), "cp1252"
                lines = text.splitlines()
                new_lines = [self._playlist_line(ln, win_root) for ln in lines]
                if new_lines == lines:
                    continue
                nl = "\r\n" if "\r\n" in text else "\n"
                data = nl.join(new_lines) + (nl if text.endswith(("\n", "\r")) else "")
                try:
                    out = data.encode(enc)
                except UnicodeEncodeError:
                    out = data.encode("utf-8")
                if raw.startswith(codecs.BOM_UTF8) and not out.startswith(codecs.BOM_UTF8):
                    out = codecs.BOM_UTF8 + out
                self.log.write({"op": "rewrite", "path": name, "data": base64.b64encode(raw).decode("ascii")})
                tmp = self.abs(name) + ".tc-tmp"
                with open(tmp, "wb") as fh:
                    fh.write(out)
                os.replace(tmp, self.abs(name))
                self.done["playlists"] += 1
            except Exception as e:
                self.err(f"Playlist {name}", e)

    def cleanup_empty_dirs(self):
        cands = set()
        for d in self.touched_dirs:
            while d and d != ".":
                cands.add(d)
                d = os.path.dirname(d)
        for d in sorted(cands, key=lambda x: -x.count(os.sep)):
            p = self.abs(d)
            try:
                if os.path.isdir(p) and not os.listdir(p):
                    os.rmdir(p)
                    self.log.write({"op": "rmdir", "path": d})
                    self.done["rmdirs"] += 1
            except OSError:
                pass


# ===========================================================================
def list_undo():
    if not os.path.isdir(UNDO_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(UNDO_DIR), reverse=True):
        if not fn.endswith(".jsonl"):
            continue
        p = os.path.join(UNDO_DIR, fn)
        try:
            with open(p, encoding="utf-8") as fh:
                header = json.loads(fh.readline())
                n = sum(1 for _ in fh)
        except Exception:
            continue
        out.append({"name": fn[:-6], "root": header.get("root"), "time": header.get("time"),
                    "summary": header.get("summary"), "ops": n,
                    "undone": os.path.exists(p + ".undone")})
    return out


def undo(name, progress):
    p = os.path.join(UNDO_DIR, os.path.basename(name) + ".jsonl")
    with open(p, encoding="utf-8") as fh:
        lines = [json.loads(line) for line in fh if line.strip()]
    root = lines[0]["root"]
    ops = lines[1:]
    errors = []
    created_dirs, recreated = set(), set()
    for i, op in enumerate(reversed(ops)):
        progress("Rückgängig machen", i + 1, len(ops), op.get("src") or op.get("path") or op.get("dst"), "Aktuelle Datei")
        try:
            if op["op"] == "move":
                src, dst = os.path.join(root, op["src"]), os.path.join(root, op["dst"])
                if os.path.exists(src) and not (os.path.exists(dst) and os.path.samefile(src, dst)):
                    raise FileExistsError(op["src"])
                os.makedirs(os.path.dirname(src), exist_ok=True)
                if os.path.exists(src):  # nur Schreibweise
                    os.rename(dst, dst + ".tc-tmp")
                    dst = dst + ".tc-tmp"
                shutil.move(dst, src)
                created_dirs.add(os.path.dirname(op["dst"]))
            elif op["op"] == "tags":
                full = os.path.join(root, op["path"])
                tf = open_tags(full)
                tf.restore(op["snap"], remove_cover=op.get("cover_added", False),
                           pics=_load_pics(op["pics"]) if op.get("pics") else None)
                if op.get("times"):
                    os.utime(full, tuple(op["times"]))
            elif op["op"] == "utime":
                os.utime(os.path.join(root, op["path"]), tuple(op["times"]))
            elif op["op"] == "rewrite":
                with open(os.path.join(root, op["path"]), "wb") as fh:
                    fh.write(base64.b64decode(op["data"]))
            elif op["op"] == "create":
                os.remove(os.path.join(root, op["path"]))
                created_dirs.add(os.path.dirname(op["path"]))
            elif op["op"] == "rmdir":
                os.makedirs(os.path.join(root, op["path"]), exist_ok=True)
                recreated.add(op["path"])
        except Exception as e:
            errors.append(f"{op['op']} {op.get('path') or op.get('dst')}: {e}")
    # durch den Lauf entstandene, jetzt leere Ordner entfernen
    cands = set()
    for d in created_dirs:
        while d and d != ".":
            cands.add(d)
            d = os.path.dirname(d)
    for d in sorted(cands - recreated, key=lambda x: -x.count(os.sep)):
        try:
            full = os.path.join(root, d)
            if os.path.isdir(full) and not os.listdir(full):
                os.rmdir(full)
        except OSError:
            pass
    with open(p + ".undone", "w") as fh:
        fh.write(str(time.time()))
    return len(ops), errors
