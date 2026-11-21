"""Tüm yanıtlara eklenen güvenlik başlıkları."""

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
