import json

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import Field

from .tools import Parameters


class Folder(Parameters):
    title: str = Field(min_length=1, max_length=120)
    parent: int | None = Field(default=None, gt=0)


class Thread(Parameters):
    title: str = Field(min_length=1, max_length=120)
    folder: int | None = Field(default=None, gt=0)


class UpdateThread(Parameters):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    folder: int | None = Field(default=None, gt=0)
    pinned: bool | None = None
    archived: bool | None = None


def owns(store, table, identifier, account):
    row = store.one(f"SELECT * FROM {table} WHERE id=? AND user_id=?", (identifier, account))
    if not row:
        raise HTTPException(404, "The requested record does not exist")
    return row


def require_title(title):
    title = title.strip()
    if not title:
        raise ValueError("Enter a title")
    return title


def history(store, identifier, limit=150, before=None):
    condition = "AND id<?" if before is not None else ""
    args = (identifier, before, limit) if before is not None else (identifier, limit)
    rows = store.all(
        "SELECT id,message,created FROM messages WHERE conversation_id=? " + condition + " ORDER BY id DESC LIMIT ?",
        args,
    )
    return [{"id": row["id"], "created": row["created"], **json.loads(row["message"])} for row in reversed(rows)]


def routes(s, authentication):
    router = APIRouter(prefix="/api")
    store = s.store

    @router.get("/folders")
    def folder_list(account=Depends(authentication.account)):
        return store.all("SELECT id,title,parent FROM folders WHERE user_id=? ORDER BY title", (account,))

    @router.post("/folders", status_code=201)
    def folder_create(body: Folder, account=Depends(authentication.account)):
        if body.parent is not None:
            owns(store, "folders", body.parent, account)
        title = require_title(body.title)
        identifier = store.run("INSERT INTO folders(user_id,parent,title) VALUES (?,?,?)", (account, body.parent, title))
        return {"id": identifier, "title": title, "parent": body.parent}

    @router.patch("/folders/{identifier}")
    def folder_edit(identifier: int, body: Folder, account=Depends(authentication.account)):
        owns(store, "folders", identifier, account)
        ancestor = body.parent
        while ancestor is not None:
            if ancestor == identifier:
                raise ValueError("A folder cannot be moved into itself or a descendant")
            ancestor = owns(store, "folders", ancestor, account)["parent"]
        store.run("UPDATE folders SET title=?,parent=? WHERE id=? AND user_id=?",
                  (require_title(body.title), body.parent, identifier, account))
        return owns(store, "folders", identifier, account)

    @router.delete("/folders/{identifier}", status_code=204)
    def folder_delete(identifier: int, account=Depends(authentication.account)):
        row = owns(store, "folders", identifier, account)
        with store.connect() as db:
            db.execute("UPDATE folders SET parent=? WHERE parent=? AND user_id=?", (row["parent"], identifier, account))
            db.execute("UPDATE conversations SET folder=? WHERE folder=? AND user_id=?",
                       (row["parent"], identifier, account))
            db.execute("DELETE FROM folders WHERE id=? AND user_id=?", (identifier, account))

    @router.get("/conversations")
    def conversations(q: str = Query(default="", max_length=200), archived: bool = False,
                      offset: int = Query(default=0, ge=0), account=Depends(authentication.account)):
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        return store.all(
            "SELECT c.* FROM conversations c WHERE c.user_id=? AND c.archived=? AND "
            "(?='' OR c.title LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM messages m WHERE "
            "m.conversation_id=c.id AND m.role IN ('assistant','user') AND m.message LIKE ? ESCAPE '\\')) "
            "ORDER BY c.pinned DESC,c.id DESC LIMIT 200 OFFSET ?",
            (account, archived, q, like, like, offset),
        )

    @router.post("/conversations", status_code=201)
    def create(body: Thread, account=Depends(authentication.account)):
        if body.folder:
            owns(store, "folders", body.folder, account)
        title = require_title(body.title)
        identifier = store.run(
            "INSERT INTO conversations(user_id,title,folder,created) VALUES (?,?,?,CURRENT_TIMESTAMP)",
            (account, title, body.folder),
        )
        return owns(store, "conversations", identifier, account)

    @router.patch("/conversations/{identifier}")
    def update(identifier: int, body: UpdateThread, account=Depends(authentication.account)):
        old = owns(store, "conversations", identifier, account)
        if body.folder:
            owns(store, "folders", body.folder, account)
        current = {**old, **body.model_dump(exclude_unset=True)}
        current["title"] = require_title(current["title"] or old["title"])
        for key in ("pinned", "archived"):
            if current[key] is None:
                current[key] = old[key]
        store.run("UPDATE conversations SET title=?,folder=?,pinned=?,archived=? WHERE id=? AND user_id=?",
                  (current["title"], current["folder"], current["pinned"], current["archived"], identifier, account))
        return owns(store, "conversations", identifier, account)

    @router.get("/conversations/{identifier}")
    def get(identifier: int, before: int | None = None, account=Depends(authentication.account)):
        row = owns(store, "conversations", identifier, account)
        return {"thread": row, "messages": history(store, identifier, before=before)}

    @router.get("/conversations/{identifier}/export")
    def export(identifier: int, account=Depends(authentication.account)):
        return JSONResponse(
            {"thread": owns(store, "conversations", identifier, account),
             "messages": history(store, identifier, limit=-1)},
            headers={"Content-Disposition": f"attachment; filename=bud-conversation-{identifier}.json"},
        )

    @router.delete("/conversations/{identifier}", status_code=204)
    async def delete(identifier: int, derived: bool = False, account=Depends(authentication.account)):
        owns(store, "conversations", identifier, account)
        active = s.runner.active.get(account)
        if active and active.conversation == identifier:
            raise HTTPException(409, "Stop the conversation before deleting it")
        with store.connect() as db:
            if derived:
                db.execute("DELETE FROM memories WHERE conversation=? AND user_id=?", (identifier, account))
                db.execute("DELETE FROM agent_runs WHERE conversation=? AND user_id=?", (identifier, account))
                db.execute("DELETE FROM errors WHERE conversation=? AND user_id=?", (identifier, account))
            db.execute("DELETE FROM messages WHERE conversation_id=?", (identifier,))
            db.execute("DELETE FROM conversations WHERE id=? AND user_id=?", (identifier, account))
        if derived:
            for row in store.all("SELECT id FROM artifacts WHERE conversation=? AND user_id=?", (identifier, account)):
                s.artifacts.remove(account, row["id"])

    return router
