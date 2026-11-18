"""Giriş noktası:  uvicorn app:app

Ayarlar tamamsa asıl uygulamayı (core.py) çalıştırır. Değilse ilk açılış
kurulum sihirbazını gösterir; kurulum bitince sunucuyu yeniden başlatmaya
gerek kalmadan asıl uygulamaya geçer.
"""
import threading

import config_store


class _Dispatcher:
    def __init__(self):
        self._core = None
        self._setup = None
        self._lock = threading.Lock()
        if config_store.is_configured():
            self._load_core()
        else:
            self._load_setup()

    def _load_core(self):
        with self._lock:
            if self._core is None:
                config_store.apply_to_env()
                import core  # ayarlar ortama aktarıldıktan sonra yüklenmeli
                self._core = core.app
                config_store.clear_setup_token()
        return self._core

    def _load_setup(self):
        if self._setup is None:
            import setup_wizard
            self._setup = setup_wizard.app
            token = config_store.get_setup_token()
            line = "=" * 60
            print(f"\n{line}\n  İLK KURULUM GEREKİYOR\n"
                  f"  Tarayıcıda uygulamayı aç ve bu kurulum kodunu gir:\n\n"
                  f"      {token}\n\n"
                  f"  (Kod ayrıca {config_store.SETUP_TOKEN_PATH} dosyasında.)\n{line}\n",
                  flush=True)
        return self._setup

    async def __call__(self, scope, receive, send):
        if self._core is None and config_store.is_configured():
            self._load_core()
        target = self._core or self._load_setup()
        await target(scope, receive, send)


app = _Dispatcher()
