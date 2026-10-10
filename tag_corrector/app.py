"""Weboberfläche (Flask) + Hintergrund-Jobs."""
import json
import os
import re
import threading
import time
import traceback

from flask import Flask, jsonify, request, send_from_directory

from . import executor, planner
from .mb import CACHE_DIR, MusicBrainz
from .util import windows_to_wsl

STATIC = os.path.join(os.path.dirname(__file__), "static")
SETTINGS_FILE = os.path.join(CACHE_DIR, "settings.json")

app = Flask(__name__, static_folder=None)


class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.settings = dict(planner.DEFAULT_SETTINGS)
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as fh:
                saved = json.load(fh)
            # Werte, die beim Speichern noch dem damaligen Standard entsprachen, folgen neuen Standards
            old_defaults = saved.pop("_defaults", {})
            for k, v in planner.DEFAULT_SETTINGS.items():
                if k in saved and k in old_defaults and saved[k] == old_defaults[k] and saved[k] != v:
                    saved[k] = v
            self.settings.update(saved)
        except Exception:
            pass
        self.mb = MusicBrainz()
        self.lib = None
        self.groups = {}
        self.matches = {}
        self.albums = {}
        self.misc = []
        self.stale = False
        self.plan_settings = None
        self.job = {"kind": None, "running": False}
        self.stop = False
        self.skip_mb = False

    STEPS = {"scan": ["Dateien suchen", "Tags lesen", "MusicBrainz-Abgleich", "Plan erstellen"],
             "apply": ["Änderungen schreiben", "Aufräumen"],
             "undo": ["Rückgängig machen"]}

    def progress(self, phase, done, total, msg="", label="Aktuelles Lied"):
        with self.lock:
            if self.job.get("phase") != phase:
                self.job["phase_started"] = time.time()
            steps = self.STEPS.get(self.job.get("kind"), [])
            self.job.update({"phase": phase, "done": done, "total": total, "msg": msg, "label": label,
                             "step": steps.index(phase) + 1 if phase in steps else None,
                             "steps": len(steps)})

    def start_job(self, kind, fn):
        with self.lock:
            if self.job.get("running"):
                return False
            self.stop = False
            self.skip_mb = False
            self.job = {"kind": kind, "running": True, "started": time.time(), "phase": "Start",
                        "phase_started": time.time(), "done": 0, "total": 0, "msg": "", "error": None,
                        "result": None}

        def runner():
            try:
                res = fn()
                with self.lock:
                    self.job["result"] = res
            except InterruptedError:
                with self.lock:
                    self.job["error"] = "Abgebrochen"
            except Exception as e:
                traceback.print_exc()
                with self.lock:
                    self.job["error"] = str(e)
            finally:
                with self.lock:
                    self.job["running"] = False
                    self.job["finished"] = time.time()

        threading.Thread(target=runner, daemon=True).start()
        return True


ST = State()


# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.route("/static/<path:p>")
def static_files(p):
    return send_from_directory(STATIC, p)


@app.get("/api/settings")
def get_settings():
    return jsonify(ST.settings)


@app.get("/api/ls")
def ls():
    p = windows_to_wsl(request.args.get("path", ""))
    base, prefix = (p, "") if p.endswith("/") or os.path.isdir(p) and p.endswith(os.sep) else os.path.split(p)
    out = []
    try:
        for e in sorted(os.listdir(base or "/"))[:500]:
            if e.lower().startswith(prefix.lower()) and os.path.isdir(os.path.join(base or "/", e)) \
                    and not e.startswith("."):
                out.append(os.path.join(base or "/", e))
    except OSError:
        pass
    return jsonify(out[:50])


@app.post("/api/scan")
def scan():
    S = dict(planner.DEFAULT_SETTINGS)
    S.update(request.json or {})
    S["root"] = os.path.abspath(windows_to_wsl(S["root"]))
    S["subdir"] = (S.get("subdir") or "").strip().strip("/")
    if not os.path.isdir(S["root"]):
        return jsonify({"error": f"Ordner nicht gefunden: {S['root']}"}), 400
    ST.settings = S
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
        json.dump(dict(S, _defaults=planner.DEFAULT_SETTINGS), fh, ensure_ascii=False, indent=1)

    def job():
        lib = planner.scan(S, ST.progress, lambda: ST.stop)
        groups = planner.group_files(lib)
        leftovers = planner.assign_extras(groups, lib)
        matches = {}
        n_songs = sum(len(g["files"]) for g in groups)
        if S["use_mb"]:
            songs_done = 0
            for i, g in enumerate(groups):
                if ST.stop:
                    raise InterruptedError()
                h = g["hints"] = planner._hints(g)
                ST.progress("MusicBrainz-Abgleich", songs_done, n_songs,
                            f"{h['tag_artist'] or h['folder_artist'] or '?'} – {h['tag_album'] or h['folder_album'] or '?'}"
                            f"  ({len(g['files'])} Lieder, Album {i + 1} von {len(groups)})", "Aktuelles Album")
                songs_done += len(g["files"])
                if ST.skip_mb or not (h["tag_album"] or h["folder_album"]):
                    continue
                try:
                    matches[g["id"]] = planner.match_group(g, ST.mb, S)
                except Exception as e:
                    matches[g["id"]] = {"release": None, "conf": 0, "candidates": [], "error": str(e)}
        albums = {}
        songs_done = 0
        for i, g in enumerate(groups):
            ST.progress("Plan erstellen", songs_done, n_songs, g["files"][0]["rel"], "Aktuelles Lied")
            songs_done += len(g["files"])
            albums[g["id"]] = planner.plan_album(g, matches.get(g["id"]), S)
            if (err := (matches.get(g["id"]) or {}).get("error")):
                albums[g["id"]]["warnings"].append("MusicBrainz-Fehler: " + err)
        ST.progress("Plan erstellen", n_songs, n_songs, "Duplikate prüfen", "Schritt")
        planner.resolve_collisions(albums, S)
        misc = planner.misc_section(lib, leftovers, S)
        with ST.lock:
            ST.lib, ST.groups, ST.matches = lib, {g["id"]: g for g in groups}, matches
            ST.albums, ST.misc, ST.stale, ST.plan_settings = albums, misc, False, S
        return {"files": len(lib["files"]), "albums": len(albums),
                "mb_requests": ST.mb.requests, "mb_cache": ST.mb.cache_hits}

    if not ST.start_job("scan", job):
        return jsonify({"error": "Es läuft bereits ein Vorgang"}), 409
    return jsonify({"ok": True})


@app.get("/api/status")
def status():
    with ST.lock:
        j = dict(ST.job)
    j["now"] = time.time()
    return jsonify(j)


@app.post("/api/stop")
def stop():
    mode = (request.json or {}).get("mode", "abort")
    if mode == "skip_mb":
        ST.skip_mb = True
    else:
        ST.stop = True
    return jsonify({"ok": True})


def _album_summary(a):
    return {"id": a["id"], "album": a["target"]["album"], "albumartist": a["target"]["albumartist"],
            "year": a["target"]["year"], "status": a["status"], "confidence": a["confidence"],
            "flags": a["flags"], "n_tracks": len(a["tracks"]), "n_changed": a["n_changed_tracks"],
            "has_changes": a["has_changes"], "n_items": len(a["items"]), "dir": a["dir"],
            "source_dirs": a["source_dirs"], "warnings": a["warnings"],
            "default_on": a["status"] not in ("mb_unsure", "unknown")}


@app.get("/api/plan")
def plan():
    with ST.lock:
        if ST.plan_settings is None:
            return jsonify({"empty": True})
        artists = {}
        for a in ST.albums.values():
            artists.setdefault(a["target"]["albumartist"], []).append(_album_summary(a))
        out = []
        for name in sorted(artists, key=lambda s: s.lower()):
            albs = sorted(artists[name], key=lambda x: (x["year"] or "9999", x["album"].lower()))
            out.append({"name": name, "albums": albs})
        stats = {"files": len(ST.lib["files"]), "albums": len(ST.albums), "artists": len(out),
                 "broken": [{"rel": f["rel"], "error": f["error"]} for f in ST.lib["files"] if f.get("error")][:100],
                 "changed_albums": sum(1 for a in ST.albums.values() if a["has_changes"]),
                 "changed_tracks": sum(a["n_changed_tracks"] for a in ST.albums.values()),
                 "status": {}, "flags": {}}
        for a in ST.albums.values():
            stats["status"][a["status"]] = stats["status"].get(a["status"], 0) + 1
            for f in a["flags"]:
                stats["flags"][f] = stats["flags"].get(f, 0) + 1
        return jsonify({"root": ST.plan_settings["root"], "settings": ST.plan_settings, "stale": ST.stale,
                        "artists": out, "misc": ST.misc, "stats": stats})


@app.get("/api/album/<aid>")
def album(aid):
    with ST.lock:
        a = ST.albums.get(aid)
        if not a:
            return jsonify({"error": "unbekannt"}), 404
        return jsonify(a)


@app.post("/api/album/<aid>/rematch")
def rematch(aid):
    body = request.json or {}
    with ST.lock:
        g = ST.groups.get(aid)
        if not g or ST.job.get("running"):
            return jsonify({"error": "Nicht möglich (unbekanntes Album oder Vorgang läuft)"}), 409
        S = ST.plan_settings
        old = ST.albums[aid]
        ov = dict(old.get("overrides") or {})
    try:
        match = ST.matches.get(aid)
        if "overrides" in body:
            ov.update({k: (v or "").strip() for k, v in body["overrides"].items()})
            ov = {k: v for k, v in ov.items() if v}
        if body.get("offline"):
            ov["offline"] = True
        elif body.get("mbid") or body.get("query"):
            ov.pop("offline", None)
            mbid = None
            if body.get("mbid"):
                m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", body["mbid"].lower())
                if not m:
                    return jsonify({"error": "Keine gültige MusicBrainz-Release-ID/URL"}), 400
                mbid = m.group(0)
            S_mb = dict(S, use_mb=True)
            new_match = planner.match_group(g, ST.mb, S_mb, forced_mbid=mbid, query=body.get("query"))
            if mbid and not new_match.get("release"):
                return jsonify({"error": "Release bei MusicBrainz nicht gefunden"}), 404
            if not mbid and match:
                # alte Kandidaten behalten, neue vorne
                seen = {c["id"] for c in new_match.get("candidates", [])}
                new_match["candidates"] += [c for c in match.get("candidates", []) if c["id"] not in seen]
            match = new_match
            if mbid:
                ov = {k: v for k, v in ov.items() if k not in ("album", "albumartist", "year")}
        elif body.get("reset"):
            ov = {}
        S_eff = dict(S, use_mb=True) if match and match.get("release") else S
        a = planner.plan_album(g, match, S_eff, ov)
        with ST.lock:
            ST.matches[aid] = match
            ST.albums[aid] = a
            planner.resolve_collisions(ST.albums, S)
        return jsonify({"album": a, "summary": _album_summary(a)})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


def _selection(raw):
    """Auswahl aus der Oberfläche ({album_id: {"exclude_tracks": [...], "exclude_items": [...]}})
    -> {album_id: {"tracks": [...], "items": [...]}}. Aufruf mit ST.lock."""
    sel = {}
    for aid, ex in raw.items():
        a = ST.albums.get(aid)
        if not a:
            continue
        xt, xi = set(ex.get("exclude_tracks", [])), set(ex.get("exclude_items", []))
        sel[aid] = {"tracks": [t["id"] for t in a["tracks"] if t["id"] not in xt and t["has_changes"]],
                    "items": [i["id"] for i in a["items"] if i["id"] not in xi]}
    return sel


@app.post("/api/apply/preview")
def apply_preview():
    """Zählt, was ein Lauf mit dieser Auswahl und diesen Kategorien konkret tun würde."""
    body = request.json or {}
    cats = body.get("categories", {})
    with ST.lock:
        if ST.plan_settings is None:
            return jsonify({"error": "Kein Plan"}), 409
        sel = _selection(body.get("selection", {}))
        c = {"tags": 0, "junk": 0, "moves": 0, "dups": 0, "cover_embed": 0, "cover_fix": 0,
             "extras": 0, "cover_files": 0, "carry": 0}
        unsure = []
        for aid, s in sel.items():
            a = ST.albums[aid]
            tids, iids = set(s["tracks"]), set(s["items"])
            touched = False
            for t in a["tracks"]:
                if t["id"] not in tids:
                    continue
                n0 = sum(c.values())
                c["tags"] += bool(cats.get("tags") and t["write"])
                c["junk"] += bool(cats.get("junk") and t["junk"])
                c["cover_embed"] += bool(cats.get("cover") and t["embed_cover"])
                c["cover_fix"] += bool(cats.get("cover") and t.get("fix_cover"))
                c["dups"] += bool(cats.get("dups") and t["dup"])
                c["moves"] += bool(cats.get("structure") and not t["dup"] and t["dst"] != t["src"])
                touched |= sum(c.values()) > n0
            for it in a["items"]:
                if it["id"] in iids:
                    k = {"quarantine": "extras", "cover": "cover_files", "carry": "carry"}[it["kind"]]
                    on = cats.get({"extras": "extras", "cover_files": "cover", "carry": "structure"}[k])
                    c[k] += bool(on)
                    touched |= bool(on)
            if touched and a["status"] in ("mb_unsure", "unknown"):
                unsure.append(f"{a['target']['albumartist']} – {a['target']['album']}")
    return jsonify({"counts": c, "albums": len(sel), "unsure": unsure,
                    "misc": len(body.get("misc", [])), "touch": bool(cats.get("touch"))})


@app.post("/api/apply")
def apply():
    body = request.json or {}
    raw = body.get("selection", {})
    cats = body.get("categories", {})
    misc_ids = set(body.get("misc", []))
    with ST.lock:
        if ST.plan_settings is None or ST.stale:
            return jsonify({"error": "Kein aktueller Plan – bitte neu scannen"}), 409
        S = ST.plan_settings
        albums = list(ST.albums.values())
        misc = [m for m in ST.misc if m["id"] in misc_ids]
        sel = _selection(raw)
    cats["quarantine"] = S["quarantine"]
    n_tracks = sum(len(v.get("tracks", [])) for v in sel.values())

    def job():
        log = executor.UndoLog(S["root"], {"tracks": n_tracks, "albums": len(sel), "categories": cats})
        try:
            ex = executor.Executor(S["root"], log, ST.progress, lambda: ST.stop)
            done, errors = ex.run(albums, misc, sel, cats, ST.mb)
        finally:
            log.close()
        with ST.lock:
            ST.stale = True
        return {"done": done, "errors": errors, "undo": log.name}

    if not ST.start_job("apply", job):
        return jsonify({"error": "Es läuft bereits ein Vorgang"}), 409
    return jsonify({"ok": True})


@app.get("/api/undo")
def undo_list():
    return jsonify(executor.list_undo())


@app.post("/api/undo/<name>")
def undo_run(name):
    def job():
        n, errors = executor.undo(name, ST.progress)
        with ST.lock:
            ST.stale = True
        return {"ops": n, "errors": errors}

    if not ST.start_job("undo", job):
        return jsonify({"error": "Es läuft bereits ein Vorgang"}), 409
    return jsonify({"ok": True})


def main():
    import argparse
    ap = argparse.ArgumentParser(description="tag-corrector 🐢")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    print(f"\n  🐢  tag-corrector läuft auf  http://{args.host}:{args.port}\n")
    app.run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
