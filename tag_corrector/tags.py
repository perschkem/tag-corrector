"""Einheitliches Lesen/Schreiben von Tags für alle gängigen Formate (mutagen).

Normalisierte Felder:
  title, artist, album, albumartist, track, tracktotal, disc, disctotal,
  year, genre, compilation, mb_trackid, mb_albumid, mb_releasegroupid,
  mb_artistid, mb_albumartistid
"""
import base64
import os
import pickle
import re
import struct

import mutagen
from mutagen.apev2 import APEv2, APENoHeaderError
from mutagen.asf import ASF
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (APIC, ID3, TALB, TCMP, TCON, TDRC, TIT2, TPE1, TPE2,
                         TPOS, TRCK, TXXX, UFID)
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

from .util import normalize_picture, picture_info, to_int, year_of

FIELDS = ["title", "artist", "album", "albumartist", "track", "tracktotal",
          "disc", "disctotal", "year", "genre", "compilation"]
MB_FIELDS = ["mb_trackid", "mb_albumid", "mb_releasegroupid", "mb_artistid", "mb_albumartistid"]

LOSSLESS_EXTS = {".flac", ".wav", ".aif", ".aiff", ".ape", ".wv"}

# Schlüsselwörter für Müll-Tags (Kommentare, Encoder, URLs, Ripper-Werbung)
_JUNK_WORDS = re.compile(r"comment|descr|encod|url|www|http|ripp|source|tool|cddb|itunes_cddb|"
                         r"purchase|owner|catalog_?url|copyright_?url|tagging|tagged", re.I)
_KEEP_TXXX = re.compile(r"^(musicbrainz|replaygain|acoustid|isrc|barcode|catalognumber|"
                        r"originalyear|originaldate|artists|asin|script|releasetype|"
                        r"releasestatus|releasecountry|label|media|totaldiscs|totaltracks)", re.I)


def _txt(v):
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        v = [str(x) for x in v if str(x).strip()]
        return "; ".join(v) if v else None
    s = str(v).strip()
    return s or None


def _short(v, n=80):
    s = _txt(v) or ""
    return s if len(s) <= n else s[: n - 1] + "…"


class TagFile:
    """Basisklasse. Unterklassen implementieren _read/_write/_snapshot/_restore."""
    kind = "?"
    writable = True

    def __init__(self, path, f):
        self.path = path
        self.f = f

    # --- öffentliche API -------------------------------------------------
    def read(self):
        data = {k: None for k in FIELDS + MB_FIELDS}
        junk = []
        has_cover = False
        if self.f.tags is not None:
            data, junk, has_cover = self._read()
        info = self.f.info
        ext = os.path.splitext(self.path)[1].lower()
        lossless = ext in LOSSLESS_EXTS or getattr(info, "codec", "") == "alac"
        return {
            "tags": data,
            "junk": junk,
            "has_cover": has_cover,
            "length": round(getattr(info, "length", 0) or 0, 1),
            "bitrate": int(getattr(info, "bitrate", 0) or 0),
            "lossless": lossless,
            "kind": self.kind,
            "writable": self.writable,
            "pics": self.picture_infos() if has_cover else [],
        }

    def picture_infos(self):
        """Größe/Format aller eingebetteten Bilder (für die Planung)."""
        try:
            return [dict(picture_info(data), mime=mime) for data, mime in self._pics_get()]
        except Exception:
            return []

    def picture_state(self):
        """Alle eingebetteten Bilder als Bytes (für Undo)."""
        return pickle.dumps(self._pics_state())

    def snapshot(self):
        return base64.b64encode(pickle.dumps(self._snapshot())).decode()

    def restore(self, snap, remove_cover=False, pics=None):
        if pics is not None:  # Bilder zuerst zurücksetzen, _restore lässt sie dann stehen und speichert
            if self.f.tags is None:
                self.f.add_tags()
            self._pics_restore(pickle.loads(pics))
            remove_cover = False
        self._restore(pickle.loads(base64.b64decode(snap)), remove_cover)

    def write(self, values, remove_junk=True, cover=None, fix_pics=0):
        """values: dict normalisierter Felder (None = Feld löschen, fehlend = nicht anfassen).
        fix_pics: max. Kantenlänge; zu große/kaputte eingebettete Bilder werden ersetzt."""
        if self.f.tags is None:
            self.f.add_tags()
        if fix_pics:
            self._pics_apply(lambda data, mime: normalize_picture(data, mime, fix_pics))
        self._write(values, remove_junk, cover)


# ---------------------------------------------------------------------------
class ID3File(TagFile):
    kind = "ID3"

    def read(self):
        r = super().read()
        if self.f.tags is not None:
            r["kind"] = "ID3v2.%d" % self.f.tags.version[1]
        return r

    _MAP = {"title": "TIT2", "artist": "TPE1", "album": "TALB", "albumartist": "TPE2", "genre": "TCON"}
    _MBTXXX = {"mb_albumid": "MusicBrainz Album Id", "mb_releasegroupid": "MusicBrainz Release Group Id",
               "mb_artistid": "MusicBrainz Artist Id", "mb_albumartistid": "MusicBrainz Album Artist Id"}

    def _read(self):
        t = self.f.tags
        d = {k: None for k in FIELDS + MB_FIELDS}
        for k, fid in self._MAP.items():
            if fid in t:
                d[k] = _txt(t[fid].text)
        if "TRCK" in t:
            a, _, b = str(t["TRCK"].text[0]).partition("/")
            d["track"], d["tracktotal"] = to_int(a), to_int(b)
        if "TPOS" in t:
            a, _, b = str(t["TPOS"].text[0]).partition("/")
            d["disc"], d["disctotal"] = to_int(a), to_int(b)
        for fid in ("TDRC", "TYER", "TDOR", "TORY"):
            if fid in t and t[fid].text:
                d["year"] = year_of(str(t[fid].text[0]))
                if d["year"]:
                    break
        if "TCMP" in t:
            d["compilation"] = str(t["TCMP"].text[0]) == "1"
        for k, desc in self._MBTXXX.items():
            key = "TXXX:" + desc
            if key in t:
                d[k] = _txt(t[key].text)
        uf = t.get("UFID:http://musicbrainz.org")
        if uf:
            d["mb_trackid"] = uf.data.decode("ascii", "ignore")

        junk = []
        for key, frame in t.items():
            fid = frame.FrameID
            if fid == "COMM":
                junk.append({"key": "Kommentar" + (f" ({frame.desc})" if frame.desc else ""),
                             "value": _short(frame.text), "comment": True})
            elif fid in ("TENC", "TSSE", "PRIV", "GEOB", "TDEN", "TDTG", "TOFN", "TFLT", "TMED") or \
                    (fid.startswith("W") and fid != "WXXX") or fid == "WXXX":
                val = getattr(frame, "url", None) or getattr(frame, "text", None) or getattr(frame, "owner", "")
                junk.append({"key": fid, "value": _short(val), "comment": False})
            elif fid == "TXXX" and not _KEEP_TXXX.match(frame.desc) and _JUNK_WORDS.search(frame.desc):
                junk.append({"key": "TXXX:" + frame.desc, "value": _short(frame.text),
                             "comment": "comment" in frame.desc.lower()})
        # ID3v1 / APEv2 Altlasten
        if self._has_id3v1():
            junk.append({"key": "ID3v1-Tag", "value": "veraltet, wird entfernt", "comment": False})
        if self._has_ape():
            junk.append({"key": "APEv2-Tag", "value": "in MP3 unüblich, wird entfernt", "comment": False})
        has_cover = any(k.startswith("APIC") for k in t.keys())
        return d, junk, has_cover

    def _has_id3v1(self):
        if not self.path.lower().endswith(".mp3"):
            return False
        try:
            with open(self.path, "rb") as fh:
                fh.seek(-128, 2)
                return fh.read(3) == b"TAG"
        except OSError:
            return False

    def _has_ape(self):
        if not self.path.lower().endswith(".mp3"):
            return False
        try:
            APEv2(self.path)
            return True
        except Exception:
            return False

    def _write(self, v, remove_junk, cover):
        t = self.f.tags
        for k, fid in self._MAP.items():
            if k in v:
                t.delall(fid)
                if v[k]:
                    cls = {"TIT2": TIT2, "TPE1": TPE1, "TALB": TALB, "TPE2": TPE2, "TCON": TCON}[fid]
                    t.add(cls(encoding=3, text=[v[k]]))
        if "track" in v:
            t.delall("TRCK")
            if v["track"]:
                s = str(v["track"]) + (f"/{v['tracktotal']}" if v.get("tracktotal") else "")
                t.add(TRCK(encoding=3, text=[s]))
        if "disc" in v:
            t.delall("TPOS")
            if v["disc"]:
                s = str(v["disc"]) + (f"/{v['disctotal']}" if v.get("disctotal") else "")
                t.add(TPOS(encoding=3, text=[s]))
        if "year" in v:
            for fid in ("TDRC", "TYER", "TDAT", "TIME", "TRDA"):
                t.delall(fid)
            if v["year"]:
                t.add(TDRC(encoding=3, text=[str(v["year"])]))
        if "compilation" in v:
            t.delall("TCMP")
            if v["compilation"]:
                t.add(TCMP(encoding=3, text=["1"]))
        for k, desc in self._MBTXXX.items():
            if v.get(k):
                t.delall("TXXX:" + desc)
                t.add(TXXX(encoding=3, desc=desc, text=[v[k]]))
        if v.get("mb_trackid"):
            t.delall("UFID:http://musicbrainz.org")
            t.add(UFID(owner="http://musicbrainz.org", data=v["mb_trackid"].encode()))
        if remove_junk:
            for key, frame in list(t.items()):
                fid = frame.FrameID
                if fid in ("COMM", "TENC", "TSSE", "PRIV", "GEOB", "TDEN", "TDTG", "TOFN", "TFLT", "TMED") \
                        or fid.startswith("W") \
                        or (fid == "TXXX" and not _KEEP_TXXX.match(frame.desc) and _JUNK_WORDS.search(frame.desc)):
                    del t[key]
        if cover:
            data, mime = cover
            t.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
        if self.path.lower().endswith(".mp3"):
            self._save_v23(v1=0 if remove_junk else 1)
            if remove_junk and self._has_ape():
                from mutagen.apev2 import delete as ape_delete
                ape_delete(self.path)
        else:
            self.f.save()

    def _save_v23(self, **kw):
        """Als ID3v2.3 speichern: Android-Player (z.B. Musicolet) lesen das Jahr nur aus TYER,
        nicht aus TDRC (v2.4), und zeigen UTF-8-Texte (nur v2.4) falsch an; mutagen schreibt
        sie in v2.3 als UTF-16. Sortier-Frames bleiben erhalten."""
        t = self.f.tags
        keep = [fr for fid in ("TSOA", "TSOP", "TSOT") for fr in t.getall(fid)]
        t.update_to_v23()
        for fr in keep:
            t.add(fr)
        self.f.save(v2_version=3, **kw)

    def _snapshot(self):
        return [f for f in self.f.tags.values() if f.FrameID != "APIC"] if self.f.tags is not None else []

    def _pics_get(self):
        return [(fr.data, fr.mime) for fr in self.f.tags.getall("APIC")] if self.f.tags is not None else []

    def _pics_apply(self, fn):
        t = self.f.tags
        frames = t.getall("APIC")
        t.delall("APIC")
        for fr in frames:
            r = fn(fr.data, fr.mime)
            if r is None:
                t.add(fr)
            elif r:
                fr.data, fr.mime = r
                t.add(fr)

    def _pics_state(self):
        return self.f.tags.getall("APIC") if self.f.tags is not None else []

    def _pics_restore(self, frames):
        self.f.tags.delall("APIC")
        for fr in frames:
            self.f.tags.add(fr)

    def _restore(self, frames, remove_cover):
        if self.f.tags is None:
            self.f.add_tags()
        t = self.f.tags
        for key in list(t.keys()):
            if not key.startswith("APIC") or remove_cover:
                del t[key]
        for fr in frames:
            t.add(fr)
        if self.path.lower().endswith(".mp3"):
            self.f.save(v2_version=4)
        else:
            self.f.save()


# ---------------------------------------------------------------------------
class VorbisFile(TagFile):
    """FLAC, Ogg Vorbis, Opus, Ogg FLAC."""
    kind = "Vorbis"

    _MAP = {"title": "title", "artist": "artist", "album": "album", "albumartist": "albumartist",
            "genre": "genre", "mb_trackid": "musicbrainz_trackid", "mb_albumid": "musicbrainz_albumid",
            "mb_releasegroupid": "musicbrainz_releasegroupid", "mb_artistid": "musicbrainz_artistid",
            "mb_albumartistid": "musicbrainz_albumartistid"}
    _JUNK_KEYS = {"comment", "description", "encoder", "encoded-by", "encodedby", "encoded_by",
                  "encodersettings", "encoding", "url", "www", "wwwaudiofile", "purl", "source",
                  "ripper", "rippedby", "ripping tool", "tool", "itunes_cddb_1", "cddb", "contact",
                  "website", "ufid", "tagger", "tagging time", "taggingdate"}

    def _get(self, key):
        t = self.f.tags
        return _txt(t.get(key)) if key in t else None

    def _read(self):
        d = {k: None for k in FIELDS + MB_FIELDS}
        for k, key in self._MAP.items():
            d[k] = self._get(key)
        if not d["albumartist"]:
            d["albumartist"] = self._get("album artist")
        tn = self._get("tracknumber") or ""
        a, _, b = tn.partition("/")
        d["track"] = to_int(a)
        d["tracktotal"] = to_int(b) or to_int(self._get("tracktotal")) or to_int(self._get("totaltracks"))
        dn = self._get("discnumber") or ""
        a, _, b = dn.partition("/")
        d["disc"] = to_int(a)
        d["disctotal"] = to_int(b) or to_int(self._get("disctotal")) or to_int(self._get("totaldiscs"))
        d["year"] = year_of(self._get("date")) or year_of(self._get("year")) or year_of(self._get("originaldate"))
        comp = self._get("compilation")
        d["compilation"] = comp == "1" if comp is not None else None
        junk = []
        for key, val in self.f.tags.items():
            lk = key.lower()
            if lk in self._JUNK_KEYS or (_JUNK_WORDS.search(lk) and not _KEEP_TXXX.match(lk)):
                junk.append({"key": key.upper(), "value": _short(val),
                             "comment": lk in ("comment", "description")})
        has_cover = bool(getattr(self.f, "pictures", None)) or "metadata_block_picture" in self.f.tags \
            or "coverart" in self.f.tags
        return d, junk, has_cover

    def _write(self, v, remove_junk, cover):
        t = self.f.tags
        def setk(key, val):
            if key in t:
                del t[key]
            if val not in (None, ""):
                t[key] = [str(val)]
        for k, key in self._MAP.items():
            if k in v and (v[k] or not k.startswith("mb_")):
                setk(key, v[k])
        if "albumartist" in v:
            setk("album artist", None)
        if "track" in v:
            setk("tracknumber", v["track"])
            setk("tracktotal", v.get("tracktotal"))
            setk("totaltracks", None)
        if "disc" in v:
            setk("discnumber", v["disc"])
            setk("disctotal", v.get("disctotal"))
            setk("totaldiscs", None)
        if "year" in v:
            setk("date", v["year"])
            setk("year", None)
        if "compilation" in v:
            setk("compilation", "1" if v["compilation"] else None)
        if remove_junk:
            for key in list(t.keys()):
                lk = key.lower()
                if lk in self._JUNK_KEYS or (_JUNK_WORDS.search(lk) and not _KEEP_TXXX.match(lk)):
                    del t[key]
        if cover:
            data, mime = cover
            pic = Picture()
            pic.type, pic.mime, pic.desc, pic.data = 3, mime, "Cover", data
            if isinstance(self.f, FLAC):
                self.f.add_picture(pic)
            else:
                t["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
        self.f.save()

    def _snapshot(self):
        items = [(k, v) for k, v in self.f.tags.items() if k.lower() != "metadata_block_picture"] \
            if self.f.tags is not None else []
        return items

    def _ogg_pics(self):
        out = []
        for b in (self.f.tags.get("metadata_block_picture") or []) if self.f.tags is not None else []:
            try:
                out.append(Picture(base64.b64decode(b)))
            except Exception:
                pic = Picture()
                pic.type, pic.data = 3, b""
                out.append(pic)
        return out

    def _pics_get(self):
        pics = self.f.pictures if isinstance(self.f, FLAC) else self._ogg_pics()
        return [(p.data, p.mime) for p in pics]

    def _pics_apply(self, fn):
        flac = isinstance(self.f, FLAC)
        pics = list(self.f.pictures) if flac else self._ogg_pics()
        keep = []
        for p in pics:
            r = fn(p.data, p.mime)
            if r is None:
                keep.append(p)
            elif r:
                info = picture_info(r[0])
                p.data, p.mime, p.width, p.height = r[0], r[1], info["w"], info["h"]
                p.depth, p.colors = 24, 0
                keep.append(p)
        self._set_pics(keep)

    def _set_pics(self, pics):
        if isinstance(self.f, FLAC):
            self.f.clear_pictures()
            for p in pics:
                self.f.add_picture(p)
        elif pics:
            self.f.tags["metadata_block_picture"] = [base64.b64encode(p.write()).decode("ascii") for p in pics]
        elif "metadata_block_picture" in self.f.tags:
            del self.f.tags["metadata_block_picture"]

    def _pics_state(self):
        return list(self.f.pictures) if isinstance(self.f, FLAC) else self._ogg_pics()

    def _pics_restore(self, pics):
        self._set_pics(pics)

    def _restore(self, items, remove_cover):
        if self.f.tags is None:
            self.f.add_tags()
        t = self.f.tags
        for key in list(t.keys()):
            if key.lower() != "metadata_block_picture" or remove_cover:
                del t[key]
        for k, val in items:
            t[k] = val
        if remove_cover and isinstance(self.f, FLAC):
            self.f.clear_pictures()
        self.f.save()


# ---------------------------------------------------------------------------
class MP4File(TagFile):
    kind = "MP4"
    _MAP = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb", "albumartist": "aART",
            "genre": "\xa9gen", "year": "\xa9day"}
    _FF = "----:com.apple.iTunes:"
    _MBFF = {"mb_trackid": "MusicBrainz Track Id", "mb_albumid": "MusicBrainz Album Id",
             "mb_releasegroupid": "MusicBrainz Release Group Id", "mb_artistid": "MusicBrainz Artist Id",
             "mb_albumartistid": "MusicBrainz Album Artist Id"}
    _JUNK = {"\xa9cmt": ("Kommentar", True), "\xa9too": ("Encoder", False), "desc": ("Beschreibung", True),
             "ldes": ("Lange Beschreibung", True), "apID": ("Käufer-Account", False),
             "ownr": ("Besitzer", False), "purd": ("Kaufdatum", False), "\xa9enc": ("Encoded by", False),
             "purl": ("URL", False)}

    def _read(self):
        t = self.f.tags
        d = {k: None for k in FIELDS + MB_FIELDS}
        for k, key in self._MAP.items():
            if key in t:
                d[k] = _txt(t[key])
        d["year"] = year_of(d["year"])
        if "trkn" in t and t["trkn"]:
            d["track"], d["tracktotal"] = (t["trkn"][0] + (0, 0))[:2]
            d["track"], d["tracktotal"] = d["track"] or None, d["tracktotal"] or None
        if "disk" in t and t["disk"]:
            d["disc"], d["disctotal"] = (t["disk"][0] + (0, 0))[:2]
            d["disc"], d["disctotal"] = d["disc"] or None, d["disctotal"] or None
        if "cpil" in t:
            d["compilation"] = bool(t["cpil"])
        for k, name in self._MBFF.items():
            key = self._FF + name
            if key in t and t[key]:
                d[k] = bytes(t[key][0]).decode("utf-8", "ignore")
        junk = []
        for key, val in t.items():
            if key in self._JUNK:
                label, is_comment = self._JUNK[key]
                junk.append({"key": label, "value": _short(val), "comment": is_comment})
            elif key.startswith(self._FF):
                name = key[len(self._FF):]
                if _JUNK_WORDS.search(name) and not _KEEP_TXXX.match(name):
                    junk.append({"key": name, "value": _short([bytes(x).decode('utf-8', 'ignore') for x in val]),
                                 "comment": "comment" in name.lower()})
        return d, junk, "covr" in t

    def _is_junk(self, key):
        if key in self._JUNK:
            return True
        if key.startswith(self._FF):
            name = key[len(self._FF):]
            return bool(_JUNK_WORDS.search(name) and not _KEEP_TXXX.match(name))
        return False

    def _write(self, v, remove_junk, cover):
        t = self.f.tags
        for k, key in self._MAP.items():
            if k in v:
                if key in t:
                    del t[key]
                if v[k]:
                    t[key] = [str(v[k])]
        if "track" in v:
            t.pop("trkn", None)
            if v["track"]:
                t["trkn"] = [(int(v["track"]), int(v.get("tracktotal") or 0))]
        if "disc" in v:
            t.pop("disk", None)
            if v["disc"]:
                t["disk"] = [(int(v["disc"]), int(v.get("disctotal") or 0))]
        if "compilation" in v:
            t.pop("cpil", None)
            if v["compilation"]:
                t["cpil"] = True
        for k, name in self._MBFF.items():
            if v.get(k):
                t[self._FF + name] = [MP4FreeForm(v[k].encode("utf-8"))]
        if remove_junk:
            for key in list(t.keys()):
                if self._is_junk(key):
                    del t[key]
        if cover:
            data, mime = cover
            fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
            t["covr"] = [MP4Cover(data, imageformat=fmt)]
        self.f.save()

    def _snapshot(self):
        return {k: v for k, v in self.f.tags.items() if k != "covr"} if self.f.tags is not None else {}

    def _pics_get(self):
        covr = self.f.tags.get("covr", []) if self.f.tags is not None else []
        return [(bytes(c), None) for c in covr]

    def _pics_apply(self, fn):
        t = self.f.tags
        new = []
        for c in t.get("covr", []):
            r = fn(bytes(c), None)
            if r is None:
                new.append(c)
            elif r:
                fmt = MP4Cover.FORMAT_PNG if r[1] == "image/png" else MP4Cover.FORMAT_JPEG
                new.append(MP4Cover(r[0], imageformat=fmt))
        self._pics_restore(new)

    def _pics_state(self):
        return [(bytes(c), c.imageformat) for c in self.f.tags.get("covr", [])] if self.f.tags is not None else []

    def _pics_restore(self, pics):
        covers = [c if isinstance(c, MP4Cover) else MP4Cover(c[0], imageformat=c[1]) for c in pics]
        if covers:
            self.f.tags["covr"] = covers
        else:
            self.f.tags.pop("covr", None)

    def _restore(self, items, remove_cover):
        if self.f.tags is None:
            self.f.add_tags()
        t = self.f.tags
        for key in list(t.keys()):
            if key != "covr" or remove_cover:
                del t[key]
        for k, val in items.items():
            t[k] = val
        self.f.save()


# ---------------------------------------------------------------------------
class ASFFile(TagFile):
    kind = "WMA"
    _MAP = {"title": "Title", "artist": "Author", "album": "WM/AlbumTitle", "albumartist": "WM/AlbumArtist",
            "genre": "WM/Genre", "mb_trackid": "MusicBrainz/Track Id", "mb_albumid": "MusicBrainz/Album Id",
            "mb_releasegroupid": "MusicBrainz/Release Group Id", "mb_artistid": "MusicBrainz/Artist Id",
            "mb_albumartistid": "MusicBrainz/Album Artist Id"}
    _JUNK = {"Description": True, "WM/EncodedBy": False, "WM/EncodingSettings": False, "WM/ToolName": False,
             "WM/ToolVersion": False, "WM/PromotionURL": False, "WM/AuthorURL": False, "WM/UserWebURL": False,
             "WM/AudioFileURL": False, "WM/AudioSourceURL": False, "WM/Provider": False,
             "WM/MediaClassPrimaryID": False, "WM/MediaClassSecondaryID": False, "WM/WMContentID": False,
             "WM/WMCollectionID": False, "WM/WMCollectionGroupID": False}

    def _g(self, key):
        t = self.f.tags
        return _txt([str(x) for x in t[key]]) if key in t else None

    def _read(self):
        d = {k: None for k in FIELDS + MB_FIELDS}
        for k, key in self._MAP.items():
            d[k] = self._g(key)
        d["track"] = to_int(self._g("WM/TrackNumber"))
        a, _, b = (self._g("WM/PartOfSet") or "").partition("/")
        d["disc"], d["disctotal"] = to_int(a), to_int(b)
        d["year"] = year_of(self._g("WM/Year")) or year_of(self._g("WM/OriginalReleaseYear"))
        junk = [{"key": k, "value": _short([str(x) for x in self.f.tags[k]]), "comment": c}
                for k, c in self._JUNK.items() if k in self.f.tags]
        return d, junk, "WM/Picture" in self.f.tags

    def _write(self, v, remove_junk, cover):
        t = self.f.tags
        def setk(key, val):
            if key in t:
                del t[key]
            if val not in (None, ""):
                t[key] = [str(val)]
        for k, key in self._MAP.items():
            if k in v and (v[k] or not k.startswith("mb_")):
                setk(key, v[k])
        if "track" in v:
            setk("WM/TrackNumber", v["track"])
        if "disc" in v:
            setk("WM/PartOfSet", (f"{v['disc']}/{v['disctotal']}" if v.get("disctotal") else v["disc"]) if v["disc"] else None)
        if "year" in v:
            setk("WM/Year", v["year"])
        if "compilation" in v:
            setk("WM/IsCompilation", "1" if v["compilation"] else None)
        if remove_junk:
            for k in self._JUNK:
                if k in t:
                    del t[k]
        if cover:
            data, mime = cover
            t["WM/Picture"] = [_asf_pic(3, mime, "", data)]
        self.f.save()

    def _pics_get(self):
        pics = [_asf_unpic(bytes(a.value)) for a in self.f.tags.get("WM/Picture", [])] \
            if self.f.tags is not None else []
        return [(data, mime) for _, mime, _, data in pics]

    def _pics_apply(self, fn):
        t = self.f.tags
        new = []
        for a in t.get("WM/Picture", []):
            typ, mime, desc, data = _asf_unpic(bytes(a.value))
            r = fn(data, mime)
            if r is None:
                new.append(a)
            elif r:
                new.append(_asf_pic(typ, r[1], desc, r[0]))
        self._pics_restore(new)

    def _pics_state(self):
        return list(self.f.tags.get("WM/Picture", [])) if self.f.tags is not None else []

    def _pics_restore(self, pics):
        if pics:
            self.f.tags["WM/Picture"] = list(pics)
        elif "WM/Picture" in self.f.tags:
            del self.f.tags["WM/Picture"]

    def _snapshot(self):
        return [(k, v) for k, v in self.f.tags.items() if k != "WM/Picture"] if self.f.tags is not None else []

    def _restore(self, items, remove_cover):
        t = self.f.tags
        for key in list(t.keys()):
            if key != "WM/Picture" or remove_cover:
                del t[key]
        grouped = {}
        for k, val in items:
            grouped.setdefault(k, []).append(val)
        for k, vals in grouped.items():
            t[k] = vals
        self.f.save()


# ---------------------------------------------------------------------------
class APEFile(TagFile):
    """Monkey's Audio, WavPack, Musepack (APEv2-Tags)."""
    kind = "APEv2"
    _MAP = {"title": "Title", "artist": "Artist", "album": "Album", "albumartist": "Album Artist",
            "genre": "Genre", "mb_trackid": "MUSICBRAINZ_TRACKID", "mb_albumid": "MUSICBRAINZ_ALBUMID",
            "mb_releasegroupid": "MUSICBRAINZ_RELEASEGROUPID", "mb_artistid": "MUSICBRAINZ_ARTISTID",
            "mb_albumartistid": "MUSICBRAINZ_ALBUMARTISTID"}

    def _g(self, key):
        t = self.f.tags
        return _txt(str(t[key]).split("\x00")) if key in t else None

    def _read(self):
        d = {k: None for k in FIELDS + MB_FIELDS}
        for k, key in self._MAP.items():
            d[k] = self._g(key)
        a, _, b = (self._g("Track") or "").partition("/")
        d["track"], d["tracktotal"] = to_int(a), to_int(b)
        a, _, b = (self._g("Disc") or "").partition("/")
        d["disc"], d["disctotal"] = to_int(a), to_int(b)
        d["year"] = year_of(self._g("Year"))
        junk = [{"key": k, "value": _short(str(v)), "comment": k.lower() == "comment"}
                for k, v in self.f.tags.items()
                if _JUNK_WORDS.search(k) and not _KEEP_TXXX.match(k) and not k.lower().startswith("cover art")]
        return d, junk, any(k.lower().startswith("cover art") for k in self.f.tags.keys())

    def _write(self, v, remove_junk, cover):
        t = self.f.tags
        def setk(key, val):
            if key in t:
                del t[key]
            if val not in (None, ""):
                t[key] = str(val)
        for k, key in self._MAP.items():
            if k in v and (v[k] or not k.startswith("mb_")):
                setk(key, v[k])
        if "track" in v:
            setk("Track", (f"{v['track']}/{v['tracktotal']}" if v.get("tracktotal") else v["track"]) if v["track"] else None)
        if "disc" in v:
            setk("Disc", (f"{v['disc']}/{v['disctotal']}" if v.get("disctotal") else v["disc"]) if v["disc"] else None)
        if "year" in v:
            setk("Year", v["year"])
        if "compilation" in v:
            setk("Compilation", "1" if v["compilation"] else None)
        if remove_junk:
            for k in list(t.keys()):
                if _JUNK_WORDS.search(k) and not _KEEP_TXXX.match(k) and not k.lower().startswith("cover art"):
                    del t[k]
        if cover:
            from mutagen.apev2 import APEValue, BINARY
            data, mime = cover
            t["Cover Art (Front)"] = APEValue(b"cover.jpg\x00" + data, BINARY)
        self.f.save()

    def _snapshot(self):
        return [(k, v) for k, v in self.f.tags.items() if not k.lower().startswith("cover art")] \
            if self.f.tags is not None else []

    def _cover_keys(self):
        return [k for k in self.f.tags.keys() if k.lower().startswith("cover art")] if self.f.tags is not None else []

    def _pics_get(self):
        return [(self.f.tags[k].value.partition(b"\x00")[2], None) for k in self._cover_keys()]

    def _pics_apply(self, fn):
        from mutagen.apev2 import APEValue, BINARY
        for k in self._cover_keys():
            name, _, data = self.f.tags[k].value.partition(b"\x00")
            r = fn(data, None)
            if r is None:
                continue
            if not r:
                del self.f.tags[k]
            else:
                stem = os.path.splitext(name.decode("utf-8", "ignore"))[0] or "cover"
                ext = ".png" if r[1] == "image/png" else ".jpg"
                self.f.tags[k] = APEValue((stem + ext).encode() + b"\x00" + r[0], BINARY)

    def _pics_state(self):
        return [(k, self.f.tags[k]) for k in self._cover_keys()]

    def _pics_restore(self, pics):
        for k in self._cover_keys():
            del self.f.tags[k]
        for k, v in pics:
            self.f.tags[k] = v

    def _restore(self, items, remove_cover):
        t = self.f.tags
        for key in list(t.keys()):
            if not key.lower().startswith("cover art") or remove_cover:
                del t[key]
        for k, val in items:
            t[k] = val
        self.f.save()


# ---------------------------------------------------------------------------
def _asf_pic(typ, mime, desc, data):
    from mutagen.asf import ASFByteArrayAttribute
    blob = struct.pack("<bi", typ, len(data)) + mime.encode("utf-16-le") + b"\x00\x00" \
        + desc.encode("utf-16-le") + b"\x00\x00" + data
    return ASFByteArrayAttribute(blob)


def _asf_unpic(blob):
    """WM/Picture -> (typ, mime, desc, daten)."""
    typ, size = struct.unpack_from("<bi", blob, 0)
    pos = 5
    strs = []
    for _ in range(2):
        end = pos
        while blob[end:end + 2] != b"\x00\x00":
            end += 2
        strs.append(blob[pos:end].decode("utf-16-le", "ignore"))
        pos = end + 2
    return typ, strs[0], strs[1], blob[pos:pos + size]


# ---------------------------------------------------------------------------
def open_tags(path):
    """Liefert passenden TagFile-Wrapper oder None, wenn nicht lesbar."""
    try:
        f = mutagen.File(path)
    except Exception:
        return None
    if f is None:
        return None
    if isinstance(f, MP4):
        return MP4File(path, f)
    if isinstance(f, ASF):
        return ASFFile(path, f)
    tags = f.tags
    if tags is None:
        # Leere Datei ohne Tags: anhand des Typs entscheiden
        name = type(f).__name__
        if name in ("MP3", "WAVE", "AIFF", "EasyMP3"):
            return ID3File(path, f)
        if name in ("FLAC", "OggVorbis", "OggOpus", "OggFLAC", "OggSpeex"):
            return VorbisFile(path, f)
        if name in ("MonkeysAudio", "WavPack", "Musepack", "OptimFROG", "TrueAudio"):
            try:
                return APEFile(path, f)
            except APENoHeaderError:
                return None
        return None
    if isinstance(tags, ID3):
        return ID3File(path, f)
    if hasattr(tags, "vendor") or isinstance(f, FLAC):
        return VorbisFile(path, f)
    if isinstance(tags, APEv2) or type(tags).__name__ == "APEv2":
        return APEFile(path, f)
    return None
