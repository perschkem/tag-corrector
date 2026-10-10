"""Erzeugt eine absichtlich chaotische Test-Musiksammlung (benötigt ffmpeg).

    python tools/make_testdata.py /pfad/zum/testordner
"""
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mutagen.id3 import COMM, ID3, TALB, TDRC, TIT2, TPE1, TRCK, TXXX, WXXX  # noqa: E402

from tag_corrector.mb import MusicBrainz  # noqa: E402
from tag_corrector.tags import open_tags  # noqa: E402



def tone(path, seconds):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ext = os.path.splitext(path)[1]
    codec = {".mp3": ["-c:a", "libmp3lame", "-b:a", "8k"], ".flac": ["-c:a", "flac"],
             ".m4a": ["-c:a", "aac", "-b:a", "16k"], ".ogg": ["-c:a", "libvorbis", "-q:a", "0"],
             ".opus": ["-c:a", "libopus", "-b:a", "8k"]}[ext]
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"anullsrc=r=8000:cl=mono",
                    "-t", str(seconds), *codec, path], check=True)


def tag(path, **kw):
    tf = open_tags(path)
    tf.write({k: v for k, v in kw.items()}, remove_junk=False)


def tracks_of(mb, mbid):
    rel = mb.release(mbid)
    out = []
    for m in rel["media"]:
        for t in m["tracks"]:
            out.append((m["position"], t["position"], t["title"], (t.get("length") or 200000) / 1000))
    return rel, out


def main(root):
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(root)
    mb = MusicBrainz()

    # 1) OK Computer: richtiger Ort, aber Kommentare, falsches Jahr, Tippfehler, Müll-Dateien
    okc = next(r["id"] for r in mb.search_releases("OK Computer", "Radiohead") if r.get("track-count") == 12)
    rel, tr = tracks_of(mb, okc)
    d = os.path.join(root, "Radiohead", "OK Computer")
    for disc, num, title, length in tr:
        p = os.path.join(d, f"{num:02d} {title.replace('/', '-')}.mp3")
        tone(p, length)
        t = title if num != 3 else "Subterranean Homesick Alein"
        tag(p, title=t, artist="Radiohead", album="OK Computer", track=num, year="1999" if num < 5 else "1997")
        id3 = ID3(p)
        id3.add(COMM(encoding=3, lang="eng", desc="", text=["Ripped by xXx_Rippa_xXx"]))
        id3.add(WXXX(encoding=3, desc="", url="http://www.free-mp3s.example"))
        id3.add(TXXX(encoding=3, desc="Encoded by", text=["LAME super rip"]))
        id3.save()
    open(os.path.join(d, "Thumbs.db"), "wb").write(b"x")
    open(os.path.join(d, "desktop.ini"), "w").write("[x]")
    open(os.path.join(d, "folder.jpg"), "wb").write(b"\xff\xd8\xff\xe0fakejpg")
    open(os.path.join(d, "www.free-mp3s.example.url"), "w").write("[InternetShortcut]")

    # 2) Daft Punk – Discovery: falscher Ordnername, Track01-Dateinamen ohne Titel-Tags, FLAC
    rel2 = mb.search_releases("Discovery", "Daft Punk", limit=5)
    rid = next(r["id"] for r in rel2 if r.get("track-count") == 14)
    _, tr2 = tracks_of(mb, rid)
    d = os.path.join(root, "daft punk", "discovry (2001)")
    for disc, num, title, length in tr2:
        p = os.path.join(d, f"Track{num:02d}.flac")
        tone(p, length)
        tag(p, artist="Daft Punk", album="Discovery", track=num)
        tf = open_tags(p)
        tf.f.tags["COMMENT"] = "EAC Rip"
        tf.f.save()

    # 3) Lose Datei im Wurzelordner, gehört zu Nirvana – Nevermind
    p = os.path.join(root, "nirvana - smells like teen spirit.mp3")
    tone(p, 301)
    tag(p, title="Smells Like Teen Spirit", artist="Nirvana", album="Nevermind", track=1)
    # Duplikat davon an anderer Stelle (bessere Qualität: flac)
    p = os.path.join(root, "Nirvana", "Nevermind", "01 - Smells Like Teen Spirit.flac")
    tone(p, 301)
    tag(p, title="Smells Like Teen Spirit", artist="Nirvana", album="Nevermind", track=1, year="1991")
    p = os.path.join(root, "Nirvana", "Nevermind", "02 - In Bloom.flac")
    tone(p, 255)
    tag(p, title="In Bloom", artist="Nirvana", album="Nevermind", track=2, year="1991")

    # 4) Eigene Mix-CD (offline, Various Artists) als M4A
    d = os.path.join(root, "Mixe", "Sommer Mix")
    for i, (a, t) in enumerate([("Artist A", "Sonne"), ("Artist B", "Strand"), ("Artist C", "Meer")], 1):
        p = os.path.join(d, f"{i}-{t}.m4a")
        tone(p, 30 + i)
        tag(p, title=t, artist=a, album="Sommer Mix", track=i)
        tf = open_tags(p)
        tf.f.tags["\xa9cmt"] = ["gekauft bei xyz"]
        tf.f.save()

    # 5) Hauptinterpret-Album mit Gastauftritt (offline), Opus, ohne Jahr bei einem Lied
    d = os.path.join(root, "Hauptkünstler", "Mein Album")
    arts = ["Hauptkünstler", "Hauptkünstler feat. Gast", "Hauptkünstler", "Hauptkünstler"]
    for i, a in enumerate(arts, 1):
        p = os.path.join(d, f"{i:02d} - Lied {i}.opus")
        tone(p, 20 + i)
        tag(p, title=f"Lied {i}", artist=a, album="Mein Album", track=i, year=None if i == 2 else "2010")

    # 6) Müll außerhalb von Alben, leerer Ordner
    open(os.path.join(root, "playlist.m3u"), "w").write("#EXTM3U")
    open(os.path.join(root, "Radiohead", "info.nfo"), "w").write("nfo")
    os.makedirs(os.path.join(root, "Leerer Ordner", "Noch leerer"))
    print("Testsammlung erstellt in", root)


if __name__ == "__main__":
    main(sys.argv[1])
