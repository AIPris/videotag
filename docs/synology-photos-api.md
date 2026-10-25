# Synology Photos web API: what `videotag` uses

Synology does not document the Photos web API. This is what `videotag` relies on, verified against a real NAS (DS224+, DSM 7.4.1, Photos with the `SYNO.Foto.*` APIs, personal space). The undocumented calls follow captures of the Photos web UI by the [synopticon](https://github.com/fdebijl/synopticon) project (MIT), which also warns that **parameter encoding fails silently**: a malformed value produces a success response that did nothing. Verify every write by reading it back.

Not every method exists in every API version: this NAS reports `Browse.Item` v1 to v7 and `Browse.Person` v1 to v3, and `add_face` exists only in v3. If a method looks missing, check the highest version before concluding it is not there.

## Conventions

- Log in with `SYNO.API.Auth` v6, `method=login`, `account`, `passwd`, `session=FileStation`, `format=sid`, `enable_syno_token=yes`. Then send `_sid` in the form and the token in the `X-SYNO-TOKEN` header. Log out with `method=logout`.
- Calls go to `POST /webapi/entry.cgi` with `api`, `version`, `method` and parameters as a form. Lists and objects are compact JSON strings (`id=[123]`).
- Results come as `{"success": true, "data": ...}` or `{"success": false, "error": {"code": N}}`. Code `103` means the method does not exist in that version, `120` an invalid or missing parameter, `801` no permission.
- Session ids appear in download URLs: never log URLs or request errors that contain them.
- Search, listings and filters all work on the **personal space** (`SYNO.Foto.*`). The shared space is `SYNO.FotoTeam.*` and is not used.

## Reading

| Need | Call | Notes |
|---|---|---|
| All items | `SYNO.Foto.Browse.Item` `list`, `offset`, `limit` (paged, 500), `additional=["person","tag","description"]` | Each item has `id`, `type` (`photo`/`video`), `filename`, `filesize`, `time`, and the requested `additional` fields. The `person` list names everyone Photos assigned to the item, named or not, **without face positions**. |
| People | `SYNO.Foto.Browse.Person` `list`, `additional=["thumbnail"]` | `id`, `name`, `item_count`, `cover` (a face id), `additional.thumbnail.cache_key` = `<source item id>_<timestamp>` |
| Person cover crop | `SYNO.Foto.Thumbnail` `get`, `type=person`, `id=<person id>`, `size=xl`, `mode=download`, `cache_key` | Returns the JPEG face crop Photos shows for the person |
| Original file | `SYNO.Foto.Download` `download`, `unit_id=[<item id>]` | Streams the original |
| Faces on one item | `SYNO.Foto.Browse.Item` `list_face` **v7**, `id_item` | `face_id`, `person_id`, `face_bounding_box` (fractions of the image). `Person.list_face` always answered 117 here. |
| What a People page lists | `SYNO.Foto.Browse.Item` `list_with_filter`, `person=[id]`, `person_policy="or"` | Needs the `_policy` parameter; different filter categories are combined with AND |
| Search | `SYNO.Foto.Search.Search` `list_item`, `keyword` | Case-insensitive substring over file names, tag text, descriptions and person names; folds `ß` to `ss` |

## Writing

| Need | Call |
|---|---|
| Create a tag | `SYNO.Foto.Browse.GeneralTag` `create`, `name` (returns the tag id). There is no delete or rename; a tag with no items disappears from the list. |
| Attach / detach | `SYNO.Foto.Browse.Item` `add_tag` / `remove_tag`, `id=[item]`, `tag=[tag id]`. A non-empty `data.error_list` means it failed. |
| Set a description | `SYNO.Foto.Browse.Item` `set`, `id=[item]`, `description=<text>`. Stored exactly as sent (newlines, umlauts, 8,000 characters tested). Clear it with `description=""` sent as a JSON empty string (`'""'`). A value wrapped in double quotes is unwrapped. |
| Album | `SYNO.Foto.Browse.NormalAlbum` `create` (`name`), `add_item` / `delete_item` (`id=<album id>`, `item=[...]`); `SYNO.Foto.Browse.Album` `list`, `delete` (`id=[album id]`); `SYNO.Foto.Browse.Item` `list` with `album_id` lists an album's items. Albums hold photos and videos together and only reference existing items. |

## Linking a video to a person

Photos stores a person reference as a **face record** per item: a persistent face id, a bounding box, a crop thumbnail and a person link. Faces are detected once at indexing. Photos finds no faces in videos, but its manual "tag a person" feature creates a record on any item, and a video with such a record appears on the person's People page, in the video's own person list and in the person's `item_count`.

1. **Create:** `SYNO.Foto.Browse.Person` `add_face` **v3**, `id_item=<item>`, `face=[{"face_bounding_box":{"top_left":{"x":x1,"y":y1},"bottom_right":{"x":x2,"y":y2}},"face_id_temp":"<item>-0","person_id":<id>}]`. Coordinates are fractions of the image. The reply lists `face_id`.
2. **Crop:** `SYNO.Foto.Upload.Face` `upload` v1, multipart: `api`, `method`, `version` in the query string and in the form, field `face_id`, file part `file` (`"blob"`, JPEG).
3. **Verify:** `Item.list_face` v7.
4. **Remove:** `SYNO.Foto.Browse.Person` `delete_face` v1, `face_id=[id]`, `person_id`. This hard-deletes the record, and it is also what the People view's "remove" does.

Behaviour that matters:

- **Error 117 on videos without a usable thumbnail** (seen on HEVC clips whose thumbnail is broken): `add_face` answers 117 *and still stores the face*. Read the faces back and trust that, not the code.
- **Every `add_face` call creates a new record**, so calling it twice gives two faces for the same person on the video.
- **Deleting one of two duplicates drops the video from the person's page**, although the other face remains and the count does not change. Keep exactly one face per person and video.
- Uploading the crop does not change the listing; a missing crop shows the person's cover as a placeholder.

## Quirks worth knowing

- Photos' own subject ("concept") recognition may be on and still list nothing; it is not a source for objects or topics.
- Photos' Places (the map) come from GPS in the item. Without GPS a video never appears there, and `Geocoding` only lists places built from photos that have GPS.
- A listing's `additional.person` entries for unnamed groups have an empty `name` and are not returned by `Person.list`.
- HEVC videos may show broken thumbnails on a NAS without the Advanced Media Extensions package, while the originals still download fine.
