# 🐢 tag-corrector

Prüft eine Musiksammlung und bringt sie in die Form

```
<Wurzel>/<Interpret>/<Album>/01 - Titel.ext      (Mehrfach-CDs: 1-01 - Titel.ext)
```

## Starten

```bash
./start.sh
```

Dann im Browser <http://127.0.0.1:8765> öffnen. Beim ersten Start wird die Python-Umgebung automatisch angelegt.
Windows-Pfade wie `D:\Musik` werden unter WSL automatisch zu `/mnt/d/Musik`.

## Was erkannt wird

| Bereich | Details |
|---|---|
| Struktur | falsche Ordner, lose Dateien, `CD1/`-Unterordner, Dateinamen, Groß/Kleinschreibung. Beginnt der Albuminterpret mit dem Namen des vorhandenen Interpreten-Ordners („Helge Schneider und …“, „Supershirt & …“), bleibt das Album dort |
| Tags | Titel, Interpret, Album, Albuminterpret, Track/Gesamt, CD/Gesamt, **Jahr**, Genre (falls leer), Sampler-Flag, MusicBrainz-IDs |
| Kommentare & Müll | Kommentare, Encoder, URLs, „Ripped by…“, ID3v1- und APE-Reste in MP3 |
| Fremddateien | alles außer Cover-Bildern (Muster einstellbar) → `_quarantaene/` |
| Cover | Großes Bild im Ordner (`Cover.jpg`, fehlt es: Original von MusicBrainz/Cover Art Archive; bei mehreren `folder`/`cover`/`front`-Bildern gewinnt das größte). In die Dateien kommt eine verkleinerte Fassung (Standard max. 500 px). Zu große, leere oder exotische eingebettete Bilder (GIF/BMP, falscher MIME-Typ) werden ersetzt; die Originale sichert das Undo-Log |
| Duplikate | gleiches Lied mehrfach → beste Kopie (verlustfrei > Bitrate) bleibt, Rest → `_quarantaene/_duplikate/`. Live-, Remix-, Instrumental- usw. Fassungen gelten nie als Duplikat der normalen Aufnahme |
| Playlisten | `.m3u`/`.m3u8` im Wurzelordner werden beim Umbenennen/Verschieben automatisch auf die neuen Dateinamen angepasst (relative, `E:\…`- und `/mnt/…`-Pfade) |
| Beschädigte Dateien | werden in der Übersicht mit Grund aufgelistet und nicht verändert |
| Lücken | fehlende Lieder laut MusicBrainz bzw. Nummernlücken (nur Hinweis) |
| Leere Ordner | werden entfernt |

Sampler: Gibt es einen Hauptinterpreten (mehr als die Hälfte der Lieder), wird er Ordner und Albuminterpret, sonst `Various Artists`.

## So läuft es ab

1. **Prüfen**: liest alle Dateien und gleicht jedes Album mit MusicBrainz ab (ca. 1–2 s pro Album, Ergebnisse werden in `~/.cache/tag-corrector/` gespeichert, ein zweiter Lauf geht schnell). „MusicBrainz überspringen“ macht den Rest offline.
2. **Vorschau**: Interpret → Album → Lied, jeweils an- und abwählbar. Unsichere Treffer sind standardmäßig **abgewählt**; als unsicher gilt auch ein Treffer, bei dem zwei verschiedene Lieder dieselbe Tracknummer bekämen. Ein Lied wird einem MusicBrainz-Titel nur zugeordnet, wenn die Titel ausreichend ähnlich sind. Pro Album: anderes Release wählen, eigene Suche, MusicBrainz-Link einfügen oder Interpret/Album/Jahr von Hand setzen.
3. **Anwenden**: Vor dem Start zeigt eine Zusammenfassung die konkreten Zahlen (neue Tags, Umbenennungen, Cover, Quarantäne …) und warnt, wenn unsichere Alben ausgewählt sind. Die Checkbox „Änderungsdatum auf jetzt setzen“ stempelt jede geänderte oder umbenannte Datei mit dem aktuellen Datum. Ohne Haken bleibt das ursprüngliche Änderungsdatum erhalten. Nichts wird endgültig gelöscht. Jeder Lauf schreibt ein Undo-Protokoll und kann unter „Verlauf & Rückgängig“ zurückgedreht werden.

Unterstützte Formate: MP3, FLAC, M4A/AAC/ALAC, OGG, Opus, WMA, APE, WavPack, Musepack, WAV/AIFF.

Testsammlung erzeugen (braucht ffmpeg): `.venv/bin/python tools/make_testdata.py /tmp/testmusik`

Design: wie [yt-album-dl](../yt-album-dl/DESIGN.md) – Nachtblau/Gold, Pixel-Schrift (Pixelify Sans, lokal, OFL), Pixel-Rahmen, sachliche Texte.
