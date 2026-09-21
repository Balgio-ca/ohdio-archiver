#!/usr/bin/env python3
"""Archiveur personnel yottio : livres audio de Radio-Canada, via l'API publique de yodio.ca.

Commandes :
  catalog   rafraîchit le catalogue et affiche le décompte par catégorie
  list      liste les livres retenus par les filtres
  download  télécharge, renomme, étiquette et vérifie les livres retenus
  verify    revérifie l'intégrité (SHA-256 + ffprobe) de tout ce qui est archivé

Dépendances : Python 3.11+, ffmpeg et ffprobe dans le PATH. Aucune librairie externe.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import threading
import tomllib
import unicodedata
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

API = "https://yodio.ca/api"
USER_AGENT = "yottio-archiver/1.0 (archive personnelle)"
ROOT = Path(__file__).resolve().parent
MANIFEST = "manifest.json"
AGE_RE = re.compile(r"^\d+-\d+ ans$")
DURATION_TOLERANCE = 0.02  # 2 % d'écart permis entre la durée annoncée et la durée réelle

config: dict = {}
config_path: Path = ROOT / "config.toml"

# État partagé avec l'interface web.
LOG: deque[str] = deque(maxlen=400)
STOP = threading.Event()
PROGRESS: dict = {}


class Stopped(Exception):
    pass


# ---------------------------------------------------------------- utilitaires

def log(msg: str) -> None:
    print(msg, flush=True)
    stamp = datetime.now().strftime("%H:%M:%S")
    for line in msg.strip("\n").splitlines() or [""]:
        LOG.append(f"{stamp} {line}")


def check_stop() -> None:
    if STOP.is_set():
        raise Stopped()


def fold(s: str) -> str:
    """Minuscules sans accents, pour comparer les noms de catégories."""
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


def safe_name(s: str, max_len: int = 120) -> str:
    """Nom de fichier portable (macOS, Windows, exFAT) en gardant les accents."""
    s = unicodedata.normalize("NFC", s or "")
    s = s.replace("/", "-").replace("\\", "-").replace(":", " -")
    s = re.sub(r'[*?"<>|\x00-\x1f]', "", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    if len(s) > max_len:
        s = s[:max_len].rstrip(" .")
    return s or "Sans titre"


def http_get(url: str, accept: str = "application/json", retries: int = 4) -> bytes:
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == retries - 1:
                raise
            wait = 2 ** attempt * 2
            log(f"    réseau: {e} ; nouvel essai dans {wait}s")
            time.sleep(wait)
    raise RuntimeError("inaccessible")


def api(path: str):
    data = json.loads(http_get(API + path))
    time.sleep(config.get("delay", 0.5))
    return data


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def probe(path: Path) -> dict | None:
    """Retourne durée et codec audio, ou None si le fichier est illisible."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name",
         "-of", "json", str(path)],
        capture_output=True, text=True)
    if r.returncode != 0:
        return None
    info = json.loads(r.stdout or "{}")
    audio = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio:
        return None
    return {"duration": float(info.get("format", {}).get("duration") or 0), "codec": audio[0].get("codec_name")}


def duration_ok(actual: float, expected: float | None) -> bool:
    if not expected:
        return actual > 0
    return abs(actual - expected) <= max(5.0, expected * DURATION_TOLERANCE)


def download_file(url: str, dest: Path) -> None:
    """Téléchargement avec reprise (Range) et contrôle de la taille annoncée."""
    part = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(5):
        have = part.stat().st_size if part.exists() else 0
        headers = {"User-Agent": USER_AGENT}
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                if have and r.status != 206:  # le serveur ignore Range : on repart de zéro
                    have = 0
                total = int(r.headers.get("Content-Length", 0)) + have
                PROGRESS.update(bytes=have, total_bytes=total)
                with open(part, "ab" if have else "wb") as f:
                    while chunk := r.read(1 << 20):
                        f.write(chunk)
                        PROGRESS["bytes"] = PROGRESS.get("bytes", 0) + len(chunk)
                        check_stop()
            size = part.stat().st_size
            if total and size != total:
                raise IOError(f"taille {size} au lieu de {total}")
            part.replace(dest)
            return
        except urllib.error.HTTPError as e:
            if e.code == 416:  # plage invalide : fichier partiel corrompu
                part.unlink(missing_ok=True)
            elif e.code < 500:
                raise
            last = e
        except (urllib.error.URLError, TimeoutError, IOError) as e:
            last = e
        wait = 2 ** attempt * 2
        log(f"    échec ({last}) ; reprise dans {wait}s")
        time.sleep(wait)
    raise RuntimeError(f"téléchargement impossible : {url}")


# ---------------------------------------------------------------- catalogue

def category_of(show: dict) -> str:
    kicker = show.get("kicker") or ""
    return kicker.split("•", 1)[1].strip() if "•" in kicker else (kicker.strip() or "Autre")


def category_folder(cat: str) -> str:
    # Toujours deux niveaux (Catégorie/Auteur - Titre) pour que les lecteurs
    # comme Audiobookshelf reconnaissent chaque dossier comme un livre.
    return f"Jeunesse {cat}" if AGE_RE.match(cat) else cat


def matches(cat: str, names: list[str]) -> bool:
    c = fold(cat)
    for n in map(fold, names):
        if n == c or (n == "jeunesse" and AGE_RE.match(cat)):
            return True
    return False


def selected(show: dict, include: list[str], exclude: list[str]) -> bool:
    ab = config.get("audiobooks", {})
    if show["globalId"] in ab.get("force_ids", []):
        return True
    if show["globalId"] in ab.get("skip_ids", []):
        return False
    cat = category_of(show)
    if include and not matches(cat, include):
        return False
    return not matches(cat, exclude)


def load_catalog(refresh: bool) -> list[dict]:
    cache = archive_dir() / "catalogue.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text())
    log("Récupération du catalogue des livres audio…")
    shows = api("/v1/shows?key=audiobooks&region=8")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(shows, ensure_ascii=False, indent=1))
    return shows


def fetch_episodes(show_id: int) -> tuple[dict, list[dict]]:
    meta, items, seen, page = {}, [], set(), 1
    while True:
        data = api(f"/v1/episodes?id={show_id}&page={page}")
        meta = meta or data.get("meta", {})
        new = [i for i in data.get("items", []) if i["globalId"] not in seen]
        if not new:
            break
        items += new
        seen.update(i["globalId"] for i in new)
        if len(items) >= data.get("paged", {}).get("total", 0):
            break
        page += 1
    return meta, items


def archive_dir() -> Path:
    p = Path(os.environ.get("YOTTIO_ARCHIVE_DIR") or config.get("archive_dir", "Archive"))
    return p if p.is_absolute() else ROOT / p


def book_dir(show: dict) -> Path:
    author = safe_name(show.get("subtitle") or "Auteur inconnu", 80)
    title = safe_name(show["title"], 100)
    return archive_dir() / "Livres audio" / category_folder(category_of(show)) / f"{author} - {title}"


# ---------------------------------------------------------------- archivage

def tag_track(src: Path, dest: Path, cover: Path | None, tags: dict) -> None:
    """Remux sans réencodage vers .m4a avec métadonnées et pochette intégrée."""
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(src)]
    if cover:
        cmd += ["-i", str(cover), "-map", "0:a", "-map", "1:v", "-disposition:v:0", "attached_pic"]
    else:
        cmd += ["-map", "0:a"]
    cmd += ["-c", "copy"]
    for k, v in tags.items():
        if v:
            cmd += ["-metadata", f"{k}={v}"]
    cmd += ["-f", "ipod", str(dest)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg: {r.stderr.strip()[:300]}")


def archive_book(show: dict, dry_run: bool = False) -> str:
    folder = book_dir(show)
    manifest_path = folder / MANIFEST
    if manifest_path.exists():
        m = json.loads(manifest_path.read_text())
        if m.get("status") == "complet":
            if not (folder / "metadata.json").exists():
                write_abs_metadata(folder, m)
            return "déjà archivé"

    meta, items = fetch_episodes(show["globalId"])
    if not items:
        return "aucun fichier audio"
    if dry_run:
        total = sum(i.get("duration") or 0 for i in items)
        return f"{len(items)} piste(s), {total / 3600:.1f} h -> {folder.relative_to(archive_dir())}"

    folder.mkdir(parents=True, exist_ok=True)
    author = show.get("subtitle") or ""
    cat = category_of(show)
    year = (re.search(r"Publié en (\d{4})", meta.get("publisher") or "") or [None, None])[1]

    cover = folder / "cover.jpg"
    if not cover.exists() and show.get("picture"):
        try:
            download_file(show["picture"].replace("{width}", "1400").replace("{ratio}", "1x1"), cover)
        except Exception as e:
            log(f"    pochette indisponible : {e}")
    cover_ok = cover.exists() and cover.stat().st_size > 0

    (folder / "description.txt").write_text(
        "\n\n".join(filter(None, [meta.get("title"), author, meta.get("publisher"), meta.get("description"),
                                  "Source : https://ici.radio-canada.ca/ohdio" + (show.get("url") or "")])) + "\n")

    width = max(2, len(str(len(items))))
    tracks, errors = [], []
    for n, item in enumerate(items, 1):
        check_stop()
        PROGRESS.update(track=n, tracks=len(items), track_title=item["title"], bytes=0, total_bytes=0)
        name = f"{n:0{width}d} - {safe_name(item['title'], 90)}.m4a"
        dest = folder / name
        expected = item.get("duration")
        info = probe(dest) if dest.exists() else None
        if not (info and duration_ok(info["duration"], expected)):
            log(f"  [{n}/{len(items)}] {item['title']}")
            try:
                media = api(f"/v1/media?id={item['globalId']}")
                if not media.get("url"):
                    raise RuntimeError("aucune URL média")
                raw = folder / f".{item['globalId']}.src"
                download_file(media["url"], raw)
                src_info = probe(raw)
                if not src_info:
                    raise RuntimeError("fichier source illisible")
                if not duration_ok(src_info["duration"], expected):
                    raise RuntimeError(f"durée {src_info['duration']:.0f}s au lieu de {expected}s")
                tmp = folder / f".{item['globalId']}.tagged"
                tag_track(raw, tmp, cover if cover_ok else None, {
                    "title": item["title"], "album": show["title"], "artist": author,
                    "album_artist": author, "track": f"{n}/{len(items)}", "genre": f"Livre audio - {cat}",
                    "date": year, "publisher": show.get("credit"), "comment": item.get("summary"),
                    "description": meta.get("description"),
                })
                info = probe(tmp)
                if not (info and duration_ok(info["duration"], expected)):
                    raise RuntimeError("le fichier étiqueté ne passe pas la vérification")
                tmp.replace(dest)
                raw.unlink()
            except Stopped:
                raise
            except Exception as e:
                errors.append(f"{name}: {e}")
                log(f"    ERREUR {e}")
                continue
        tracks.append({"file": name, "id": item["globalId"], "title": item["title"],
                       "duration_api": expected, "duration": round(info["duration"], 2),
                       "codec": info["codec"], "size": dest.stat().st_size, "sha256": sha256(dest)})

    manifest = {
        "status": "complet" if not errors else "incomplet",
        "archived_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "id": show["globalId"], "title": show["title"], "author": author, "category": cat,
        "credit": show.get("credit"), "publisher": meta.get("publisher"), "year": year,
        "source_url": "https://ici.radio-canada.ca/ohdio" + (show.get("url") or ""),
        "cover": "cover.jpg" if cover_ok else None, "tracks": tracks, "errors": errors,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    write_abs_metadata(folder, manifest, meta.get("description"))
    return "complet" if not errors else f"incomplet ({len(errors)} erreur(s))"


def write_abs_metadata(folder: Path, m: dict, description: str | None = None) -> None:
    """Fiche metadata.json au format Audiobookshelf (prioritaire sur le nom des dossiers)."""
    if description is None:
        desc_file = folder / "description.txt"
        description = desc_file.read_text().strip() if desc_file.exists() else None
    authors = [a.strip() for a in re.split(r",| et ", m.get("author") or "") if a.strip()]
    parts = [p.strip() for p in (m.get("publisher") or "").split("|")]
    publisher = next((p for p in parts[1:2] if p and "pages" not in p), None)
    cat = m.get("category") or "Autre"
    (folder / "metadata.json").write_text(json.dumps({
        "title": m.get("title"), "subtitle": None, "authors": authors, "narrators": [], "series": [],
        "genres": [f"Jeunesse {cat}" if AGE_RE.match(cat) else cat],
        "tags": ["yottio", "Jeunesse"] if AGE_RE.match(cat) else ["yottio"],
        "publishedYear": m.get("year"), "publishedDate": None, "publisher": publisher,
        "description": description, "isbn": None, "asin": None, "language": "fr",
        "explicit": False, "abridged": False, "chapters": [],
    }, ensure_ascii=False, indent=1))


def write_index() -> None:
    """Index CSV global de tout ce qui est dans l'archive."""
    rows = []
    for m in sorted((archive_dir() / "Livres audio").rglob(MANIFEST)):
        d = json.loads(m.read_text())
        rows.append([d["category"], d["author"], d["title"], d.get("year") or "", len(d["tracks"]),
                     round(sum(t["duration"] for t in d["tracks"]) / 3600, 2), d["status"],
                     str(m.parent.relative_to(archive_dir()))])
    with open(archive_dir() / "index.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Catégorie", "Auteur", "Titre", "Année", "Pistes", "Heures", "Statut", "Dossier"])
        w.writerows(rows)


# ---------------------------------------------------------------- config

def load_config(path: Path) -> None:
    global config, config_path
    config_path = path
    if not path.exists():
        log(f"Config introuvable ({path}), utilisation de la config par défaut.")
        path = ROOT / "config.toml"
    config = tomllib.loads(path.read_text())


def toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)


def save_config() -> None:
    """Réécrit la config (appelé par l'interface web)."""
    ab = config.setdefault("audiobooks", {})
    lines = ["# Configuration de l'archiveur yottio (modifiable depuis l'interface web).",
             "# L'alias « jeunesse » regroupe les catégories 0-5, 6-8, 9-12 et 13-17 ans.", ""]
    for k in ("archive_dir", "delay", "every_hours"):
        if k in config:
            lines.append(f"{k} = {toml_value(config[k])}")
    lines += ["", "[audiobooks]"]
    for k in ("include", "exclude", "force_ids", "skip_ids"):
        lines.append(f"{k} = {toml_value(ab.get(k, []))}")
    tmp = config_path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(config_path)


# ---------------------------------------------------------------- commandes

def filters(args) -> tuple[list[str], list[str]]:
    ab = config.get("audiobooks", {})
    include = args.include if args.include is not None else ab.get("include", [])
    exclude = args.exclude if args.exclude is not None else ab.get("exclude", [])
    return include, exclude


def cmd_catalog(args) -> None:
    shows = load_catalog(refresh=True)
    include, exclude = filters(args)
    counts: dict[str, list[int]] = {}
    for s in shows:
        c = counts.setdefault(category_of(s), [0, 0])
        c[0] += 1
        c[1] += selected(s, include, exclude)
    log(f"\n{len(shows)} livres audio au catalogue\n")
    log(f"  {'Catégorie':<14}{'Total':>7}{'Retenus':>9}")
    for cat, (tot, sel) in sorted(counts.items(), key=lambda kv: -kv[1][0]):
        log(f"  {cat:<14}{tot:>7}{sel:>9}")
    log(f"\n  include={include or 'tout'}  exclude={exclude or 'rien'}")


def cmd_list(args) -> None:
    include, exclude = filters(args)
    shows = [s for s in load_catalog(args.refresh) if selected(s, include, exclude)]
    for s in shows:
        log(f"  {s['globalId']:>7}  {category_of(s):<11} {s.get('subtitle') or '':<35.35} {s['title']}")
    log(f"\n{len(shows)} livre(s) retenu(s)")


def cmd_download(args) -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"{tool} est introuvable. Installez-le avec : brew install ffmpeg")
    include, exclude = filters(args)
    shows = [s for s in load_catalog(args.refresh) if selected(s, include, exclude)]
    if args.ids:
        wanted = set(args.ids)
        shows = [s for s in load_catalog(False) if s["globalId"] in wanted]
    if args.limit:
        shows = shows[:args.limit]
    sync(shows, args.dry_run)


def sync(shows: list[dict], dry_run: bool = False) -> dict[str, int]:
    log(f"{len(shows)} livre(s) à traiter{' (simulation)' if dry_run else ''}\n")
    summary: dict[str, int] = {}
    try:
        for i, s in enumerate(shows, 1):
            PROGRESS.clear()
            PROGRESS.update(index=i, total=len(shows), id=s["globalId"], book=f"{s.get('subtitle')} - {s['title']}")
            log(f"[{i}/{len(shows)}] {s.get('subtitle')} - {s['title']} ({category_of(s)})")
            try:
                status = archive_book(s, dry_run=dry_run)
            except (KeyboardInterrupt, Stopped):
                log("\nInterrompu. Le prochain lancement reprendra où ça s'est arrêté.")
                break
            except Exception as e:
                status = f"échec : {e}"
            log(f"  -> {status}")
            key = "simulé" if dry_run else status.split(" (")[0].split(" :")[0]
            summary[key] = summary.get(key, 0) + 1
    finally:
        PROGRESS.clear()
        if not dry_run:
            write_index()
    log(f"\nRésumé : {summary}")
    return summary


def cmd_verify(args) -> None:
    bad = 0
    manifests = sorted((archive_dir() / "Livres audio").rglob(MANIFEST))
    for m in manifests:
        d = json.loads(m.read_text())
        problems = [] if d["status"] == "complet" else ["marqué incomplet"]
        for t in d["tracks"]:
            f = m.parent / t["file"]
            if not f.exists():
                problems.append(f"manquant : {t['file']}")
            elif f.stat().st_size != t["size"] or (args.deep and sha256(f) != t["sha256"]):
                problems.append(f"altéré : {t['file']}")
            elif args.deep and not probe(f):
                problems.append(f"illisible : {t['file']}")
        if problems:
            bad += 1
            log(f"PROBLÈME {m.parent.relative_to(archive_dir())}\n    " + "\n    ".join(problems))
    log(f"\n{len(manifests)} livre(s) vérifié(s), {bad} avec problème(s)"
        + ("" if args.deep else " (vérification rapide ; --deep pour SHA-256 + ffprobe)"))


def main() -> None:
    p = argparse.ArgumentParser(description="Archiveur personnel yottio")
    p.add_argument("--config", default=os.environ.get("YOTTIO_CONFIG") or str(ROOT / "config.toml"))
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_filters(sp):
        sp.add_argument("--include", nargs="*", help="catégories à inclure (remplace la config)")
        sp.add_argument("--exclude", nargs="*", help="catégories à exclure (remplace la config)")
        sp.add_argument("--refresh", action="store_true", help="rafraîchir le catalogue")

    add_filters(sub.add_parser("catalog", help="catalogue et décompte par catégorie"))
    add_filters(sub.add_parser("list", help="lister les livres retenus"))
    d = sub.add_parser("download", help="télécharger et archiver")
    add_filters(d)
    d.add_argument("--limit", type=int, help="traiter au plus N livres")
    d.add_argument("--ids", type=int, nargs="+", help="archiver seulement ces globalId")
    d.add_argument("--dry-run", action="store_true", help="afficher ce qui serait fait")
    d.add_argument("--every", type=float, default=float(os.environ.get("YOTTIO_EVERY_HOURS") or 0),
                   help="relancer toutes les N heures (0 = une seule fois)")
    v = sub.add_parser("verify", help="vérifier l'archive")
    v.add_argument("--deep", action="store_true", help="recalculer SHA-256 et relire chaque fichier")
    w = sub.add_parser("serve", help="interface web + archivage automatique")
    w.add_argument("--port", type=int, default=int(os.environ.get("YOTTIO_PORT") or 8765))

    args = p.parse_args()
    load_config(Path(args.config))
    if args.cmd == "serve":
        # web.py fait « import yottio » : il doit recevoir ce module-ci (et sa config
        # chargée), pas une seconde copie vierge importée à côté de __main__.
        sys.modules.setdefault("yottio", sys.modules[__name__])
        import web
        return web.serve(args.port)
    run = {"catalog": cmd_catalog, "list": cmd_list, "download": cmd_download, "verify": cmd_verify}[args.cmd]
    if args.cmd != "download" or not args.every:
        return run(args)
    while True:
        args.refresh = True  # nouveaux livres ajoutés au catalogue depuis le dernier passage
        run(args)
        nxt = datetime.now().astimezone().timestamp() + args.every * 3600
        log(f"\nProchain passage : {datetime.fromtimestamp(nxt).strftime('%Y-%m-%d %H:%M')}\n")
        time.sleep(args.every * 3600)


if __name__ == "__main__":
    main()
