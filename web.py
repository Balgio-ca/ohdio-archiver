"""Interface web de l'archiveur yottio + planificateur d'archivage automatique.

Aucune authentification : à n'exposer que sur le réseau local.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import yottio

lock = threading.Lock()   # un seul archivage à la fois
state = {"running": False, "job": None, "last_run": None, "next_run": None, "last_summary": None}
wake = threading.Event()  # réveille le planificateur (lancement manuel)
pending_ids: list[int] = []


def every_hours() -> float:
    return float(yottio.config.get("every_hours", 24))


def archive_statuses() -> dict[int, str]:
    out = {}
    root = yottio.archive_dir() / "Livres audio"
    if root.exists():
        for m in root.rglob(yottio.MANIFEST):
            try:
                d = json.loads(m.read_text())
                out[d["id"]] = d["status"]
            except (OSError, ValueError, KeyError):
                pass
    return out


def run_job(ids: list[int] | None = None) -> None:
    """Archive les livres retenus (ou seulement `ids`). Bloque tant que le job tourne."""
    with lock:
        yottio.STOP.clear()
        state.update(running=True, job="sélection" if ids else "complet")
        try:
            shows = yottio.load_catalog(refresh=not ids)
            if ids:
                wanted = set(ids)
                shows = [s for s in shows if s["globalId"] in wanted]
            else:
                ab = yottio.config.get("audiobooks", {})
                shows = [s for s in shows if yottio.selected(s, ab.get("include", []), ab.get("exclude", []))]
            state["last_summary"] = yottio.sync(shows)
        except Exception as e:
            yottio.log(f"ERREUR : {e}")
        finally:
            state.update(running=False, job=None, last_run=datetime.now().isoformat(timespec="seconds"))


def scheduler() -> None:
    time.sleep(5)
    while True:
        if pending_ids:
            ids = pending_ids[:]
            pending_ids.clear()
            run_job(ids)
            continue
        run_job()
        wake.clear()
        done = time.time()
        logged = None
        # La fréquence est relue à chaque tour : un changement dans l'interface s'applique tout de suite.
        while not pending_ids:
            nxt = done + every_hours() * 3600
            state["next_run"] = datetime.fromtimestamp(nxt).isoformat(timespec="minutes")
            if logged != nxt:
                yottio.log(f"Prochain passage automatique : {state['next_run'].replace('T', ' ')}")
                logged = nxt
            if time.time() >= nxt:
                break
            if wake.wait(5):
                wake.clear()
                break


def books_payload() -> dict:
    shows = yottio.load_catalog(refresh=False)
    ab = yottio.config.get("audiobooks", {})
    statuses = archive_statuses()
    books = []
    for s in shows:
        cat = yottio.category_of(s)
        books.append({
            "id": s["globalId"], "title": s["title"], "author": s.get("subtitle") or "",
            "category": cat, "jeunesse": bool(yottio.AGE_RE.match(cat)),
            "picture": (s.get("picture") or "").replace("{width}", "160").replace("{ratio}", "1x1"),
            "selected": yottio.selected(s, ab.get("include", []), ab.get("exclude", [])),
            "forced": s["globalId"] in ab.get("force_ids", []),
            "skipped": s["globalId"] in ab.get("skip_ids", []),
            "status": statuses.get(s["globalId"]),
        })
    inc, exc = ab.get("include", []), ab.get("exclude", [])
    cats: dict[str, dict] = {}
    for b in books:
        c = cats.setdefault(b["category"], {
            "name": b["category"], "count": 0, "archived": 0, "jeunesse": b["jeunesse"],
            "enabled": (not inc or yottio.matches(b["category"], inc)) and not yottio.matches(b["category"], exc)})
        c["count"] += 1
        c["archived"] += b["status"] == "complet"
    order = ["Roman", "Essai", "Biographie", "Poésie", "0-5 ans", "6-8 ans", "9-12 ans", "13-17 ans"]
    ranked = sorted(cats.values(), key=lambda c: (order.index(c["name"]) if c["name"] in order else 99, c["name"]))
    return {"books": books, "categories": ranked}


def settings_payload() -> dict:
    ab = yottio.config.get("audiobooks", {})
    return {"include": ab.get("include", []), "exclude": ab.get("exclude", []), "every_hours": every_hours()}


def status_payload() -> dict:
    return {**state, "progress": dict(yottio.PROGRESS), "log": list(yottio.LOG)[-150:],
            "queued": len(pending_ids)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def json(self, data, code: int = 200) -> None:
        self.send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.send(200, (Path(__file__).parent / "ui.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/books":
            self.json(books_payload())
        elif path == "/api/status":
            self.json(status_payload())
        elif path == "/api/settings":
            self.json(settings_payload())
        else:
            self.send(404, b"introuvable", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = self.body()
        except ValueError:
            return self.json({"error": "JSON invalide"}, 400)
        ab = yottio.config.setdefault("audiobooks", {})

        if path == "/api/settings":
            ab["include"] = [str(x) for x in data.get("include", [])]
            ab["exclude"] = [str(x) for x in data.get("exclude", [])]
            hours = float(data.get("every_hours") or 24)
            yottio.config["every_hours"] = int(hours) if hours.is_integer() else hours
            yottio.save_config()
            return self.json(settings_payload())

        if path == "/api/book":
            bid, mode = int(data["id"]), data.get("mode")  # mode : force | skip | auto
            force, skip = set(ab.get("force_ids", [])), set(ab.get("skip_ids", []))
            force.discard(bid)
            skip.discard(bid)
            if mode == "force":
                force.add(bid)
            elif mode == "skip":
                skip.add(bid)
            ab["force_ids"], ab["skip_ids"] = sorted(force), sorted(skip)
            yottio.save_config()
            return self.json({"ok": True})

        if path == "/api/run":
            ids = [int(x) for x in data.get("ids", [])]
            if ids:
                pending_ids.extend(i for i in ids if i not in pending_ids)
            elif state["running"]:
                return self.json({"error": "un archivage est déjà en cours"}, 409)
            wake.set()
            return self.json({"ok": True})

        if path == "/api/stop":
            yottio.STOP.set()
            pending_ids.clear()
            return self.json({"ok": True})

        if path == "/api/refresh":
            if state["running"]:
                return self.json({"error": "un archivage est en cours"}, 409)
            yottio.load_catalog(refresh=True)
            return self.json({"ok": True})

        self.send(404, b"introuvable", "text/plain")


def serve(port: int) -> None:
    yottio.log(f"Interface web sur le port {port}")
    threading.Thread(target=scheduler, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
