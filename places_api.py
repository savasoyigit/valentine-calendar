"""Mekanlar: gittiğimiz ve gitmek istediğimiz yerler.

Her mekanı iki kişi ayrı ayrı 1-5 yıldızla puanlar. Puanlar ayrı bir
tabloda tutulur, böylece biri puanını değiştirince diğerininki etkilenmez.
"""
from fastapi import HTTPException, Request
from pydantic import BaseModel

STATUSES = ("visited", "wishlist")


class PlaceIn(BaseModel):
    name: str
    rating: int | None = None      # giriş yapan kişinin puanı
    address: str = ""
    url: str = ""
    note: str = ""
    status: str = "visited"


class RatingIn(BaseModel):
    rating: int | None = None


def clean_place(p: PlaceIn):
    name = p.name.strip()
    if not 1 <= len(name) <= 120:
        raise HTTPException(400, "Mekan adı 1-120 karakter olmalı.")
    if p.status not in STATUSES:
        raise HTTPException(400, "Geçersiz durum.")
    url = p.url.strip()
    if url.lower().replace(" ", "").startswith(("javascript:", "data:", "vbscript:", "file:")):
        raise HTTPException(400, "Bu bağlantı türü desteklenmiyor.")
    if url and "://" not in url:
        url = "https://" + url
    if url and not url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "Bağlantı http:// veya https:// ile başlamalı.")
    rating = check_rating(p.rating)
    if p.status == "wishlist":
        rating = None
    return {
        "name": name,
        "address": p.address.strip()[:300],
        "url": url[:500],
        "note": p.note.strip()[:2000],
        "status": p.status,
    }, rating


def check_rating(rating):
    if rating is None:
        return None
    if not 1 <= int(rating) <= 5:
        raise HTTPException(400, "Puan 1 ile 5 arasında olmalı.")
    return int(rating)


def register(app, core):
    with core.db() as c:
        c.executescript("""
        create table if not exists places(
            id integer primary key,
            name text not null,
            address text default '',
            url text default '',
            note text default '',
            status text default 'visited',
            created_by text default '',
            created_at text default current_timestamp,
            updated_at text default current_timestamp
        );
        create table if not exists place_ratings(
            place_id integer not null,
            user text not null,
            rating integer not null,
            updated_at text default current_timestamp,
            primary key(place_id, user)
        );
        """)

    def set_rating(c, place_id, who, rating):
        if rating is None:
            c.execute("delete from place_ratings where place_id=? and user=?", (place_id, who))
        else:
            c.execute("""
                insert into place_ratings(place_id, user, rating) values(?,?,?)
                on conflict(place_id, user) do update set
                    rating=excluded.rating, updated_at=current_timestamp
            """, (place_id, who, rating))

    def get_place(c, i):
        row = c.execute("select * from places where id=?", (i,)).fetchone()
        if not row:
            raise HTTPException(404, "Mekan bulunamadı.")
        return row

    @app.get("/api/places")
    def list_places(r: Request):
        core.auth(r)
        with core.db() as c:
            places = [dict(x) for x in c.execute("select * from places order by id desc")]
            ratings = c.execute("select place_id, user, rating from place_ratings").fetchall()
        by_place = {}
        for x in ratings:
            by_place.setdefault(x["place_id"], {})[x["user"]] = x["rating"]
        for p in places:
            p["ratings"] = by_place.get(p["id"], {})
            values = list(p["ratings"].values())
            p["avg"] = round(sum(values) / len(values), 1) if values else None
        return places

    @app.post("/api/places")
    def add_place(b: PlaceIn, r: Request):
        who = core.auth(r)
        data, rating = clean_place(b)
        with core.db() as c:
            cur = c.execute(
                "insert into places(name,address,url,note,status,created_by) values(?,?,?,?,?,?)",
                (data["name"], data["address"], data["url"], data["note"], data["status"], who),
            )
            i = cur.lastrowid
            if rating:
                set_rating(c, i, who, rating)
        text = data["name"] + (f" · {'★' * rating}" if rating else "")
        title = (f"{who} gittiğiniz bir mekanı ekledi 📍" if data["status"] == "visited"
                 else f"{who} gidilecekler listesine bir mekan ekledi 📌")
        core.notify_other(who, "place", title, text, f"place:{i}")
        return {"id": i}

    @app.put("/api/places/{i}")
    def update_place(i: int, b: PlaceIn, r: Request):
        who = core.auth(r)
        data, rating = clean_place(b)
        with core.db() as c:
            old = get_place(c, i)
            c.execute("""
                update places set name=?, address=?, url=?, note=?, status=?,
                    updated_at=current_timestamp where id=?
            """, (data["name"], data["address"], data["url"], data["note"], data["status"], i))
            set_rating(c, i, who, rating)
        if old["status"] == "wishlist" and data["status"] == "visited":
            core.notify_other(who, "place", f"{who} bir mekanı “gittik” olarak işaretledi ✅",
                              data["name"], f"place:{i}")
        return {"ok": True}

    @app.put("/api/places/{i}/rating")
    def rate_place(i: int, b: RatingIn, r: Request):
        who = core.auth(r)
        rating = check_rating(b.rating)
        with core.db() as c:
            place = get_place(c, i)
            had = c.execute("select 1 from place_ratings where place_id=? and user=?", (i, who)).fetchone()
            if rating and place["status"] == "wishlist":
                c.execute("update places set status='visited', updated_at=current_timestamp where id=?", (i,))
            set_rating(c, i, who, rating)
        if rating and not had:
            core.notify_other(who, "place", f"{who} {place['name']} için puan verdi ⭐",
                              "★" * rating + "☆" * (5 - rating), f"place:{i}")
        return {"ok": True}

    @app.delete("/api/places/{i}")
    def delete_place(i: int, r: Request):
        core.auth(r)
        with core.db() as c:
            get_place(c, i)
            c.execute("delete from place_ratings where place_id=?", (i,))
            c.execute("delete from places where id=?", (i,))
        return {"ok": True}
