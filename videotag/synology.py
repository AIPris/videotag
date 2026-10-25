"""Thin client for the Synology Photos web API (personal space only).

Errors never include URLs or form data, because those carry the session id and the password.
"""
import contextlib
import json
import os
from pathlib import Path

import requests

from .credentials import Credentials

PAGE_SIZE = 500
TIMEOUT_SECONDS = 60
CHUNK_BYTES = 1 << 20


class SynologyError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code  # the NAS error code, when the NAS answered with one


def _id_list(*values: int) -> str:
    return json.dumps(list(values))


class PhotosClient:
    def __init__(self, creds: Credentials):
        self._creds = creds
        self._http = requests.Session()
        # Certificates are always verified. A NAS with its own certificate authority or a self-signed
        # certificate is trusted through a CA bundle file, never by switching verification off.
        self._http.verify = os.environ.get("VIDEOTAG_CA_BUNDLE") or True
        self._sid = ""
        self._token = ""

    def __enter__(self) -> "PhotosClient":
        data = self._post("/webapi/auth.cgi", {
            "api": "SYNO.API.Auth", "version": "6", "method": "login",
            "account": self._creds.username, "passwd": self._creds.password,
            "session": "FileStation", "format": "sid", "enable_syno_token": "yes",
        })
        self._sid, self._token = data["sid"], data["synotoken"]
        return self

    def __exit__(self, *exc_info) -> None:
        with contextlib.suppress(SynologyError):  # best effort: the session expires on its own
            self._post("/webapi/auth.cgi", {
                "api": "SYNO.API.Auth", "version": "6", "method": "logout",
                "session": "FileStation", "_sid": self._sid,
            })

    # -- library ---------------------------------------------------------------

    def persons(self) -> list[dict]:
        """People Photos has grouped; `additional.thumbnail.cache_key` identifies the cover face."""
        return self._paged("SYNO.Foto.Browse.Person", "list", additional='["thumbnail"]')

    def items(self, additional: tuple[str, ...] = ()) -> list[dict]:
        return self._paged("SYNO.Foto.Browse.Item", "list", additional=json.dumps(list(additional)))

    def download(self, item_id: int, dest: Path) -> Path:
        """Stream the original file of an item to `dest`."""
        return self._download({"api": "SYNO.Foto.Download", "method": "download", "unit_id": _id_list(item_id)},
                              dest, f"item {item_id}")

    def person_cover(self, person_id: int, cache_key: str, dest: Path) -> Path:
        """The face crop Photos shows as the person's cover (JPEG)."""
        return self._download({"api": "SYNO.Foto.Thumbnail", "method": "get", "mode": "download",
                               "id": str(person_id), "type": "person", "size": "xl", "cache_key": cache_key},
                              dest, f"cover of person {person_id}")

    def _download(self, params: dict, dest: Path, what: str) -> Path:
        query = {"version": "1", "_sid": self._sid, **params}
        part = dest.with_name(dest.name + ".part")
        try:
            with self._http.get(self._creds.base_url + "/webapi/entry.cgi", params=query,
                                headers=self._headers(), stream=True, timeout=TIMEOUT_SECONDS) as response:
                response.raise_for_status()
                if response.headers.get("Content-Type", "").startswith("application/json"):
                    raise SynologyError(f"download of {what} was refused")
                with part.open("wb") as out:
                    for chunk in response.iter_content(CHUNK_BYTES):
                        out.write(chunk)
        except requests.RequestException as exc:
            part.unlink(missing_ok=True)
            raise SynologyError(f"download of {what} failed ({type(exc).__name__})") from None
        part.replace(dest)
        return dest

    # -- tags ------------------------------------------------------------------

    def tags(self) -> list[dict]:
        return self._paged("SYNO.Foto.Browse.GeneralTag", "list")

    def create_tag(self, name: str) -> int:
        return self._call("SYNO.Foto.Browse.GeneralTag", "create", name=name)["tag"]["id"]

    def add_tag(self, item_id: int, tag_id: int) -> None:
        self._change_tag("add_tag", item_id, tag_id)

    def remove_tag(self, item_id: int, tag_id: int) -> None:
        self._change_tag("remove_tag", item_id, tag_id)

    def _change_tag(self, method: str, item_id: int, tag_id: int) -> None:
        data = self._call("SYNO.Foto.Browse.Item", method, id=_id_list(item_id), tag=_id_list(tag_id))
        if data.get("error_list"):
            raise SynologyError(f"{method} on item {item_id} was rejected")

    # -- descriptions ----------------------------------------------------------

    def item_description(self, item_id: int) -> str:
        data = self._call("SYNO.Foto.Browse.Item", "get", id=_id_list(item_id), additional='["description"]')
        return data["list"][0]["additional"]["description"]

    def set_description(self, item_id: int, text: str) -> None:
        """Photos stores the text exactly as sent; an empty description has to be sent as a JSON empty string."""
        self._call("SYNO.Foto.Browse.Item", "set", id=_id_list(item_id), description=text or '""')

    # -- faces -----------------------------------------------------------------
    # Photos keeps a face record per item (position, crop, person). Its manual "tag a person" feature creates
    # one on any item, including a video. Payloads follow fdebijl/synopticon's captures of the web UI.

    def item_faces(self, item_id: int) -> list[dict]:
        """Face records on one item: face_id, person_id, face_bounding_box."""
        return self._call("SYNO.Foto.Browse.Item", "list_face", version="7", id_item=item_id)["list"]

    def add_face(self, item_id: int, person_id: int, box: tuple[float, float, float, float]) -> None:
        """Create a face record for the person. Photos may answer 117 for videos without a usable thumbnail
        and still store it, so callers verify with `item_faces`."""
        x1, y1, x2, y2 = box
        entry = {"face_bounding_box": {"top_left": {"x": x1, "y": y1}, "bottom_right": {"x": x2, "y": y2}},
                 "face_id_temp": f"{item_id}-0", "person_id": person_id}
        self._call("SYNO.Foto.Browse.Person", "add_face", version="3",
                   face=json.dumps([entry], separators=(",", ":")), id_item=item_id)

    def upload_face_crop(self, face_id: int, jpeg: bytes) -> None:
        query = {"api": "SYNO.Foto.Upload.Face", "method": "upload", "version": "1", "_sid": self._sid}
        form = {"api": query["api"], "method": "upload", "version": "1", "face_id": str(face_id)}
        try:
            response = self._http.post(self._creds.base_url + "/webapi/entry.cgi", params=query, data=form,
                                       files={"file": ("blob", jpeg, "image/jpeg")}, headers=self._headers(),
                                       timeout=TIMEOUT_SECONDS)
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise SynologyError(f"face crop upload failed ({type(exc).__name__})") from None
        if not payload.get("success"):
            code = payload.get("error", {}).get("code")
            raise SynologyError(f"face crop upload failed with error code {code}", code)

    def delete_face(self, face_id: int, person_id: int) -> None:
        """Hard-deletes the face record (it is not an unassign)."""
        self._call("SYNO.Foto.Browse.Person", "delete_face", face_id=_id_list(face_id), person_id=person_id)

    def person_item_ids(self, person_id: int) -> set[int]:
        """The items Photos shows for the person (what the People view lists)."""
        rows = self._paged("SYNO.Foto.Browse.Item", "list_with_filter", person=_id_list(person_id), person_policy="or")
        return {row["id"] for row in rows}

    # -- albums ----------------------------------------------------------------

    def albums(self) -> list[dict]:
        return self._paged("SYNO.Foto.Browse.Album", "list")

    def create_album(self, name: str) -> int:
        return self._call("SYNO.Foto.Browse.NormalAlbum", "create", name=name)["album"]["id"]

    def album_item_ids(self, album_id: int) -> set[int]:
        return {item["id"] for item in self._paged("SYNO.Foto.Browse.Item", "list", album_id=album_id)}

    def add_to_album(self, album_id: int, item_ids: list[int]) -> None:
        self._change_album("add_item", album_id, item_ids)

    def remove_from_album(self, album_id: int, item_ids: list[int]) -> None:
        self._change_album("delete_item", album_id, item_ids)

    def delete_album(self, album_id: int) -> None:
        self._call("SYNO.Foto.Browse.Album", "delete", id=_id_list(album_id))

    def _change_album(self, method: str, album_id: int, item_ids: list[int]) -> None:
        data = self._call("SYNO.Foto.Browse.NormalAlbum", method, id=album_id, item=_id_list(*item_ids))
        if data.get("error_list"):
            raise SynologyError(f"{method} on album {album_id} was rejected")

    # -- plumbing --------------------------------------------------------------

    def _headers(self) -> dict:
        return {"X-SYNO-TOKEN": self._token} if self._token else {}

    def _call(self, api: str, method: str, *, version: str = "1", **params) -> dict:
        form = {"api": api, "version": version, "method": method, "_sid": self._sid, **params}
        return self._post("/webapi/entry.cgi", form)

    def _paged(self, api: str, method: str, **params) -> list[dict]:
        rows, offset = [], 0
        while True:
            page = self._call(api, method, offset=offset, limit=PAGE_SIZE, **params)["list"]
            rows += page
            if len(page) < PAGE_SIZE:
                return rows
            offset += PAGE_SIZE

    def _post(self, path: str, form: dict) -> dict:
        label = f"{form.get('api')}.{form.get('method')}"
        try:
            response = self._http.post(self._creds.base_url + path, data=form,
                                       headers=self._headers(), timeout=TIMEOUT_SECONDS)
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise SynologyError(f"{label} request failed ({type(exc).__name__})") from None
        if not payload.get("success"):
            code = payload.get("error", {}).get("code")
            raise SynologyError(f"{label} failed with error code {code}", code)
        return payload.get("data", {})
