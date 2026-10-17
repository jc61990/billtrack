"""Shared validation types and a whitelisted CRUD route factory."""
from datetime import date
from typing import Annotated, Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from .. import db
from ..recurrence import RECURRENCES
from ..scope import owner
from ..security import Principal, audit, require

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)]
Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]
Cents = Annotated[int, Field(ge=0, le=10_000_000_000)]
SCents = Annotated[int, Field(ge=-10_000_000_000, le=10_000_000_000)]
Color = Annotated[str, StringConstraints(pattern=r"^#[0-9a-fA-F]{6}$")]
Rec = Literal[RECURRENCES]  # type: ignore[valid-type]


class In(BaseModel):
    """Base input model: unknown fields rejected, control characters stripped."""
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def _no_ctrl(cls, v):
        if isinstance(v, str):
            return "".join(ch for ch in v if ch.isprintable() or ch == "\n")
        return v


def to_row(m: BaseModel) -> dict:
    out = {}
    for k, v in m.model_dump().items():
        out[k] = v.isoformat() if isinstance(v, date) else int(v) if isinstance(v, bool) else v
    return out


FK = {"account_id": "accounts", "source_id": "income_sources", "tracker_id": "trackers",
      "inv_account_id": "inv_accounts"}


def check_fks(con, row: dict):
    """Referenced rows must exist AND belong to the same user (no linking into someone else's data)."""
    for col, tbl in FK.items():
        if row.get(col) is not None and not con.execute(f"SELECT 1 FROM {tbl} WHERE id=? AND owner_id=?",
                                                        (row[col], owner())).fetchone():
            raise HTTPException(422, f"{col} does not exist")


def crud(router: APIRouter, path: str, table: str, model, read_perm: str, write_perm: str,
         order: str = "name COLLATE NOCASE", date_col: str | None = None,
         before_save: Callable | None = None, after_save: Callable | None = None):
    """Register list/create/update/delete. `table`, `order`, `date_col` are code constants, never user input."""

    @router.get(f"/{path}", name=f"list_{table}")
    def list_(start: date | None = None, end: date | None = None, p: Principal = Depends(require(read_perm))):
        with db.tx() as con:
            if date_col and start and end:
                return db.rows(con, f"SELECT * FROM {table} WHERE owner_id=? AND {date_col} BETWEEN ? AND ? "
                                    f"ORDER BY {order} LIMIT 5000", (owner(), start.isoformat(), end.isoformat()))
            return db.rows(con, f"SELECT * FROM {table} WHERE owner_id=? ORDER BY {order} LIMIT 5000", (owner(),))

    @router.post(f"/{path}", name=f"create_{table}", status_code=201)
    def create(body: model, request: Request, p: Principal = Depends(require(write_perm))):  # type: ignore
        row = to_row(body)
        with db.tx() as con:
            check_fks(con, row)
            if before_save:
                before_save(con, row, None)
            row["owner_id"] = owner()
            cur = con.execute(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                              tuple(row.values()))
            row.pop("owner_id")
            row["id"] = cur.lastrowid
            if after_save:
                after_save(con, row)
            audit(con, request, f"create {table}", row["id"])
            return row

    @router.put(f"/{path}/{{item_id}}", name=f"update_{table}")
    def update(item_id: int, body: model, request: Request, p: Principal = Depends(require(write_perm))):  # type: ignore
        row = to_row(body)
        with db.tx() as con:
            if not con.execute(f"SELECT 1 FROM {table} WHERE id=? AND owner_id=?", (item_id, owner())).fetchone():
                raise HTTPException(404, "Not found")
            check_fks(con, row)
            if before_save:
                before_save(con, row, item_id)
            cur = con.execute(f"UPDATE {table} SET {','.join(f'{k}=?' for k in row)} WHERE id=? AND owner_id=?",
                              (*row.values(), item_id, owner()))
            if not cur.rowcount:
                raise HTTPException(404, "Not found")
            row["id"] = item_id
            if after_save:
                after_save(con, row)
            audit(con, request, f"update {table}", item_id)
            return row

    @router.delete(f"/{path}/{{item_id}}", name=f"delete_{table}")
    def delete(item_id: int, request: Request, p: Principal = Depends(require(write_perm))):
        with db.tx() as con:
            if not con.execute(f"DELETE FROM {table} WHERE id=? AND owner_id=?", (item_id, owner())).rowcount:
                raise HTTPException(404, "Not found")
            audit(con, request, f"delete {table}", item_id)
        return {"ok": True}
