"""Tüm yanıtlara eklenen güvenlik başlıkları ve istek boyutu sınırı."""

# Uygulama satır içi script ve onclick kullandığı için script kaynaklarını
# kısıtlamıyoruz; ama başka sitelerin uygulamayı çerçeve içine almasını,
# eklenti nesnelerini ve formların başka adreslere gönderilmesini engelliyoruz.
CSP = "frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'"

HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    # /r/<token> gibi tek kullanımlık linklerin dış sitelere sızmasını engeller.
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
}


def _is_https(request):
    # X-Forwarded-Proto'yu yalnızca uvicorn --proxy-headers güvenilir vekilden
    # gelirse şemaya yansıtır; başlığa doğrudan güvenmiyoruz.
    return request.url.scheme == "https"


def add_security_headers(app):
    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        for name, value in HEADERS.items():
            response.headers.setdefault(name, value)
        if _is_https(request):
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        if request.url.path.startswith("/r/"):
            response.headers["Cache-Control"] = "no-store"
        return response


# ---------------------------------------------------------------- istek boyutu

DEFAULT_BODY_LIMIT = 256 * 1024          # notlar, mektuplar, formlar için fazlasıyla yeter
BODY_LIMITS = {                          # büyük gövde bekleyen uçlar
    "/api/instant-photos": 32 * 1024 * 1024,
    "/api/setup/complete": 10 * 1024 * 1024,
    "/api/settings/photo": 5 * 1024 * 1024,
}


class BodySizeLimit:
    """Tüm uygulamanın önünde duran ASGI katmanı. Gövde, sınırı aşarsa daha
    belleğe alınmadan 413 ile reddedilir; Content-Length olmayan (parçalı)
    isteklerde de okunan bayt sayılır."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)

        limit = BODY_LIMITS.get(scope["path"], DEFAULT_BODY_LIMIT)
        has_length = False
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                has_length = True
                try:
                    if int(value) > limit:
                        return await self._reject(send)
                except ValueError:
                    return await self._reject(send, 400)

        if has_length:
            # Sunucu (uvicorn/h11) Content-Length'ten fazlasını zaten kabul etmez.
            return await self.app(scope, receive, send)

        # Content-Length yok (parçalı gönderim): gövdeyi en fazla sınır kadar
        # biz okuruz; aşarsa 413, aşmazsa okuduğumuzu uygulamaya aynen veririz.
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            if len(body) > limit:
                return await self._reject(send)
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    @staticmethod
    async def _reject(send, status=413):
        body = ('{"detail":"İstek çok büyük."}' if status == 413 else '{"detail":"Geçersiz istek."}').encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
