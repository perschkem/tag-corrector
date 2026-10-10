"""MusicBrainz-Client mit Festplatten-Cache und Rate-Limit (1 Anfrage/Sekunde)."""
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://musicbrainz.org/ws/2/"
CAA = "https://coverartarchive.org/release/{}/front"   # Originalgröße
USER_AGENT = os.environ.get("TAGCORRECTOR_UA", "TagCorrector/1.0 ( local music library tool )")

CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "tag-corrector")


class MusicBrainz:
    def __init__(self, cache_dir=CACHE_DIR):
        os.makedirs(cache_dir, exist_ok=True)
        self.db_path = os.path.join(cache_dir, "musicbrainz.sqlite")
        self._lock = threading.Lock()
        self._last = 0.0
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS cache (url TEXT PRIMARY KEY, body TEXT, ts REAL)")
        self.requests = 0
        self.cache_hits = 0

    def _db(self):
        return sqlite3.connect(self.db_path, timeout=30)

    def _cached(self, url):
        with self._db() as db:
            row = db.execute("SELECT body FROM cache WHERE url=?", (url,)).fetchone()
        return json.loads(row[0]) if row else None

    def _store(self, url, data):
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (url, json.dumps(data), time.time()))

    def _throttle(self):
        with self._lock:
            wait = 1.05 - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()

    def get(self, path, params):
        params = dict(params, fmt="json")
        url = API + path + "?" + urllib.parse.urlencode(params)
        hit = self._cached(url)
        if hit is not None:
            self.cache_hits += 1
            return hit
        for attempt in range(5):
            self._throttle()
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.loads(r.read().decode("utf-8"))
                self.requests += 1
                self._store(url, data)
                return data
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    self._store(url, {})
                    return {}
                if e.code in (429, 503):
                    time.sleep(2 + attempt * 2)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(2 + attempt * 2)
        raise RuntimeError("MusicBrainz nicht erreichbar")

    # ------------------------------------------------------------------
    @staticmethod
    def _q(s):
        s = (s or "").replace("\\", " ").replace('"', " ")
        return '"' + " ".join(s.split()) + '"'

    def search_releases(self, album, artist=None, limit=10):
        q = "release:" + self._q(album)
        if artist:
            q += " AND artist:" + self._q(artist)
        data = self.get("release/", {"query": q, "limit": limit})
        return data.get("releases", []) if data else []

    def release(self, mbid):
        return self.get("release/" + mbid, {"inc": "recordings artist-credits release-groups media genres"})

    def cover(self, mbid):
        """Lädt Front-Cover in Originalgröße vom Cover Art Archive. Gibt bytes oder None."""
        cache = os.path.join(CACHE_DIR, "covers")
        os.makedirs(cache, exist_ok=True)
        p = os.path.join(cache, mbid + "-front.img")
        if os.path.exists(p):
            with open(p, "rb") as fh:
                data = fh.read()
            return data or None
        req = urllib.request.Request(CAA.format(mbid), headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
        except Exception:
            data = b""
        with open(p, "wb") as fh:
            fh.write(data)
        return data or None


def credit_name(artist_credit):
    """MusicBrainz artist-credit -> 'A feat. B'."""
    if not artist_credit:
        return ""
    return "".join(c.get("name", "") + c.get("joinphrase", "") for c in artist_credit).strip()


def credit_id(artist_credit):
    return artist_credit[0]["artist"]["id"] if artist_credit else None
