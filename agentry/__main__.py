import uvicorn

from .config import settings

uvicorn.run("agentry.app:app", host=settings.host, port=settings.port, proxy_headers=True,
            forwarded_allow_ips=settings.trusted_proxies)
