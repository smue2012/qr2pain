"""Minimaler REST-Client für paperless-ngx (getestet gegen v3.x, kompatibel mit v2)."""
from __future__ import annotations

import requests


class PaperlessError(RuntimeError):
    pass


def obtain_token(url: str, username: str, password: str,
                 verify_tls: bool | str = True, timeout: int = 30) -> str:
    """Tauscht paperless-Benutzername/Passwort gegen den API-Token des Benutzers."""
    r = requests.post(f"{url.rstrip('/')}/api/token/",
                      data={"username": username, "password": password},
                      verify=verify_tls, timeout=timeout)
    if r.status_code in (400, 401, 403):
        raise PaperlessError("Benutzername oder Passwort falsch")
    r.raise_for_status()
    return r.json()["token"]


class Paperless:
    def __init__(self, url: str, token: str, verify_tls: bool | str = True, timeout: int = 60,
                 api_version: str | None = None):
        self.base = url.rstrip("/")
        self.s = requests.Session()
        self.s.verify = verify_tls
        self.s.headers["Authorization"] = f"Token {token}"
        self.timeout = timeout
        if api_version:
            self.api_version = api_version
            self.s.headers["Accept"] = f"application/json; version={api_version}"
        else:
            self._detect_api_version()

    # ------------------------------------------------------------ intern
    def _detect_api_version(self) -> None:
        r = self.s.get(f"{self.base}/api/", timeout=self.timeout)
        if r.status_code in (401, 403):
            raise PaperlessError("Authentifizierung fehlgeschlagen – Token prüfen")
        r.raise_for_status()
        version = r.headers.get("X-Api-Version")
        if version:
            # höchste vom Server unterstützte Version explizit anfordern
            self.s.headers["Accept"] = f"application/json; version={version}"
        self.api_version = version

    def _get(self, path: str, **params):
        r = self.s.get(f"{self.base}{path}", params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _paged(self, path: str, **params):
        url, first = f"{self.base}{path}", True
        while url:
            r = self.s.get(url, params=params if first else None, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
            yield from data["results"]
            url, first = data.get("next"), False

    # ------------------------------------------------------------ Tags / Felder
    def tag_id(self, name: str, create: bool = False) -> int:
        res = self._get("/api/tags/", name__iexact=name)["results"]
        if res:
            return res[0]["id"]
        if not create:
            raise PaperlessError(f"Tag '{name}' existiert nicht")
        r = self.s.post(f"{self.base}/api/tags/", json={"name": name}, timeout=self.timeout)
        r.raise_for_status()
        return r.json()["id"]

    def custom_field_id(self, name: str) -> int | None:
        f = self.custom_field(name)
        return f["id"] if f else None

    def custom_field(self, name: str) -> dict | None:
        """Benutzerdefiniertes Feld mit id, name und data_type (Gross-/Kleinschreibung egal)."""
        res = self._get("/api/custom_fields/", name__iexact=name)["results"]
        return res[0] if res else None

    def set_custom_fields(self, doc_id: int, values: dict[int, object]) -> None:
        """Werte benutzerdefinierter Felder setzen, übrige Felder des Dokuments bleiben erhalten.

        None leert ein vorhandenes Feld (es bleibt am Dokument) bzw. fügt es gar nicht erst hinzu."""
        doc = self.get_document(doc_id)
        current = doc.get("custom_fields") or []
        present = {c["field"] for c in current}
        out = [c if c["field"] not in values else {"field": c["field"], "value": values[c["field"]]} for c in current]
        out += [{"field": k, "value": v} for k, v in values.items() if k not in present and v is not None]
        if out == current:
            return
        r = self.s.patch(f"{self.base}/api/documents/{doc_id}/", json={"custom_fields": out}, timeout=self.timeout)
        r.raise_for_status()

    # ------------------------------------------------------------ Dokumente
    def documents(self, with_tags: list[int], without_tags: list[int]):
        params = {"page_size": 100, "ordering": "created"}
        if with_tags:
            params["tags__id__all"] = ",".join(map(str, with_tags))
        if without_tags:
            params["tags__id__none"] = ",".join(map(str, without_tags))
        yield from self._paged("/api/documents/", **params)

    def download_original(self, doc_id: int) -> tuple[bytes, str]:
        r = self.s.get(f"{self.base}/api/documents/{doc_id}/download/",
                       params={"original": "true"}, timeout=self.timeout)
        r.raise_for_status()
        return r.content, r.headers.get("Content-Type", "")

    def update_tags(self, doc: dict, add: list[int], remove: list[int]) -> None:
        tags = (set(doc["tags"]) | set(add)) - set(remove)
        r = self.s.patch(f"{self.base}/api/documents/{doc['id']}/",
                         json={"tags": sorted(tags)}, timeout=self.timeout)
        r.raise_for_status()

    def get_document(self, doc_id: int) -> dict:
        return self._get(f"/api/documents/{doc_id}/")

    def preview(self, doc_id: int) -> tuple[bytes, str]:
        r = self.s.get(f"{self.base}/api/documents/{doc_id}/preview/", timeout=self.timeout)
        r.raise_for_status()
        return r.content, r.headers.get("Content-Type", "application/pdf")

    def profile(self) -> dict:
        return self._get("/api/profile/")

    def is_superuser(self) -> bool:
        try:
            return bool(self._get("/api/ui_settings/").get("user", {}).get("is_superuser"))
        except requests.RequestException:
            return False

    def visible_ids(self, ids: list[int], chunk: int = 200) -> set[int]:
        """Welche dieser Dokument-IDs darf der Benutzer in paperless sehen?

        Nutzt den Filter id__in; paperless liefert im Feld "all" alle passenden IDs,
        daher reicht eine Seite mit einem Eintrag pro Block."""
        out: set[int] = set()
        for i in range(0, len(ids), chunk):
            part = ids[i:i + chunk]
            data = self._get("/api/documents/", id__in=",".join(map(str, part)),
                             page_size=len(part), fields="id")
            found = data.get("all")
            if found is None:
                found = [r["id"] for r in data.get("results", [])]
            out.update(int(x) for x in found)
        return out

    def names(self, endpoint: str) -> dict[int, str]:
        """id -> Name für correspondents, tags, custom_fields, ..."""
        return {o["id"]: o["name"] for o in self._paged(f"/api/{endpoint}/", page_size=100)}

    def add_note(self, doc_id: int, text: str) -> None:
        r = self.s.post(f"{self.base}/api/documents/{doc_id}/notes/",
                        json={"note": text}, timeout=self.timeout)
        r.raise_for_status()
