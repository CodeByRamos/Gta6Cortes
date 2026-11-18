"""Leitores de chat ao vivo (Twitch, Kick, YouTube).

Cada leitor roda numa thread e chama `on_message(ts, user, text)` para cada
mensagem recebida, onde `ts` é o horário (time.time()) de chegada.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import ssl
import threading
import time
from urllib.parse import urlparse

import websocket

log = logging.getLogger("chat")


def _sslopt():
    ca = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    opt = {"cert_reqs": ssl.CERT_REQUIRED}
    if ca and os.path.exists(ca):
        opt["ca_certs"] = ca
    return opt


def _proxy_kwargs():
    """websocket-client não lê HTTPS_PROXY sozinho; repassamos se existir."""
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not proxy:
        return {}
    p = urlparse(proxy)
    kw = {"http_proxy_host": p.hostname, "http_proxy_port": p.port or 80, "proxy_type": "http"}
    if p.username:
        kw["http_proxy_auth"] = (p.username, p.password or "")
    return kw


class ChatReader(threading.Thread):
    def __init__(self, on_message):
        super().__init__(daemon=True)
        self.on_message = on_message
        self.stop_event = threading.Event()
        self.connected = False

    def stop(self):
        self.stop_event.set()


class _WSReader(ChatReader):
    """Base para leitores via WebSocket com reconexão automática."""

    url = ""

    def on_open(self, ws):  # pragma: no cover - sobrescrito
        pass

    def on_text(self, ws, data):  # pragma: no cover - sobrescrito
        pass

    def run(self):
        self.backoff = 2
        while not self.stop_event.is_set():
            self.ws = websocket.WebSocketApp(
                self.url,
                on_open=self._open,
                on_message=self.on_text,
                on_error=lambda ws, e: log.warning("%s erro: %s", self.name, e),
                on_close=lambda ws, *a: log.info("%s desconectado", self.name),
            )
            self.ws.run_forever(sslopt=_sslopt(), ping_interval=60, ping_timeout=20, **_proxy_kwargs())
            self.connected = False
            if self.stop_event.wait(self.backoff):
                break
            self.backoff = min(self.backoff * 2, 60)

    def _open(self, ws):
        self.connected = True
        self.backoff = 2  # conexão boa: a próxima queda reconecta rápido
        self.on_open(ws)

    def stop(self):
        super().stop()
        try:
            self.ws.close()
        except Exception:
            pass


class TwitchChat(_WSReader):
    """Chat da Twitch via IRC-over-WebSocket, modo anônimo (somente leitura)."""

    url = "wss://irc-ws.chat.twitch.tv:443"
    _PRIVMSG = re.compile(r"^(?:@\S+ )?:(\w+)!\S+ PRIVMSG #\w+ :(.*)$")

    def __init__(self, channel, on_message):
        super().__init__(on_message)
        self.channel = channel.lower()
        self.name = f"twitch-chat:{self.channel}"

    def on_open(self, ws):
        nick = f"justinfan{random.randint(10000, 99999)}"
        ws.send("CAP REQ :twitch.tv/tags")
        ws.send("PASS SCHMOOPIIE")
        ws.send(f"NICK {nick}")
        ws.send(f"JOIN #{self.channel}")
        log.info("conectado ao chat da Twitch #%s", self.channel)

    def on_text(self, ws, data):
        now = time.time()
        for line in data.split("\r\n"):
            if not line:
                continue
            if line.startswith("PING"):
                ws.send("PONG :tmi.twitch.tv")
                continue
            m = self._PRIVMSG.match(line)
            if m:
                self.on_message(now, m.group(1), m.group(2))


class KickChat(_WSReader):
    """Chat da Kick via Pusher (mesmo WebSocket usado pelo site)."""

    # A Kick troca a chave do Pusher de vez em quando (a antiga 32cbd69e... morreu em out/2026).
    # A cada conexão perguntamos a chave atual ao endpoint público do site; esta é só a reserva.
    FALLBACK_KEY, FALLBACK_CLUSTER = "34bf7a0ff419a2a775b9", "us2"
    REALTIME_URL = "https://web.kick.com/api/v1/realtime/connection"
    PARAMS = "?protocol=7&client=js&version=8.3.0&flash=false"

    @classmethod
    def pusher_credentials(cls):
        """(app_key, cluster) atuais do chat da Kick."""
        import uuid

        body = {"client": {"id": str(uuid.uuid4()), "type": "web"},
                # só oferecemos pusher: se oferecer centrifugo, a Kick escolhe ele
                "capabilities": {"accepted_providers": [{"provider": "pusher"}]}}
        try:
            try:
                from curl_cffi import requests as creq

                r = creq.post(cls.REALTIME_URL, json=body, impersonate="chrome", timeout=10,
                              headers={"Accept": "application/json"})
            except ImportError:
                import requests

                r = requests.post(cls.REALTIME_URL, json=body, timeout=10, headers={"Accept": "application/json"})
            r.raise_for_status()
            for c in r.json().get("data", {}).get("connections", []):
                cred = c.get("credentials") or {}
                key, cluster = cred.get("app_key", ""), cred.get("cluster", "")
                if c.get("provider") == "pusher" and re.fullmatch(r"[0-9a-f]{20}", key) \
                        and re.fullmatch(r"[a-z0-9-]{1,20}", cluster):
                    return key, cluster
            log.warning("Kick não devolveu chave do Pusher; usando a reserva")
        except Exception as e:
            log.warning("não consegui pegar a chave atual do chat da Kick (%s); usando a reserva", e)
        return cls.FALLBACK_KEY, cls.FALLBACK_CLUSTER

    @property
    def url(self):  # lido a cada (re)conexão
        key, cluster = self.pusher_credentials()
        return f"wss://ws-{cluster}.pusher.com/app/{key}{self.PARAMS}"

    def __init__(self, channel, on_message, chatroom_id=None):
        super().__init__(on_message)
        self.channel = channel.lower()
        self.chatroom_id = chatroom_id
        self.name = f"kick-chat:{self.channel}"

    @staticmethod
    def lookup_chatroom_id(slug):
        """Descobre o id da sala de chat pela API pública da Kick."""
        url = f"https://kick.com/api/v2/channels/{slug}"
        try:  # curl_cffi passa melhor pelo Cloudflare da Kick
            from curl_cffi import requests as creq

            r = creq.get(url, impersonate="chrome", timeout=20)
        except ImportError:
            import requests

            r = requests.get(url, headers={"Accept": "application/json"}, timeout=20)
        r.raise_for_status()
        return int(r.json()["chatroom"]["id"])

    def run(self):
        if not self.chatroom_id:
            try:
                self.chatroom_id = self.lookup_chatroom_id(self.channel)
            except Exception as e:
                log.error(
                    "não consegui descobrir o chatroom da Kick (%s). Passe --kick-chatroom-id manualmente.", e
                )
                return
        super().run()

    def on_open(self, ws):
        ws.send(json.dumps({
            "event": "pusher:subscribe",
            "data": {"auth": "", "channel": f"chatrooms.{self.chatroom_id}.v2"},
        }))
        log.info("conectado ao chat da Kick %s (sala %s)", self.channel, self.chatroom_id)

    def on_text(self, ws, data):
        now = time.time()
        try:
            msg = json.loads(data)
        except ValueError:
            return
        ev = msg.get("event", "")
        if ev == "pusher:ping":
            ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
        elif ev.endswith("ChatMessageEvent"):
            try:
                d = json.loads(msg["data"])
                self.on_message(now, d.get("sender", {}).get("username", "?"), d.get("content", ""))
            except Exception:
                pass


class YouTubeChat(ChatReader):
    """Chat do YouTube via biblioteca chat-downloader (opcional)."""

    def __init__(self, url, on_message):
        super().__init__(on_message)
        self.url = url
        self.name = "youtube-chat"

    def run(self):
        try:
            from chat_downloader import ChatDownloader
        except ImportError:
            log.error("para chat do YouTube instale: pip install chat-downloader")
            return
        while not self.stop_event.is_set():
            try:
                chat = ChatDownloader().get_chat(self.url)
                self.connected = True
                for m in chat:
                    if self.stop_event.is_set():
                        break
                    self.on_message(time.time(), m.get("author", {}).get("name", "?"), m.get("message") or "")
            except Exception as e:
                log.warning("chat do YouTube caiu: %s", e)
            self.connected = False
            self.stop_event.wait(10)


def detect_platform(url: str):
    """Retorna (plataforma, canal) a partir da URL da live."""
    u = urlparse(url if "://" in url else "https://" + url)
    host = (u.hostname or "").lower()
    parts = [p for p in u.path.split("/") if p]
    if "twitch.tv" in host:
        return "twitch", parts[0] if parts else ""
    if "kick.com" in host:
        return "kick", parts[0] if parts else ""
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube", parts[-1] if parts else ""
    return "outro", parts[0] if parts else host


def make_chat_reader(url, on_message, kick_chatroom_id=None):
    platform, channel = detect_platform(url)
    if platform == "twitch":
        return TwitchChat(channel, on_message)
    if platform == "kick":
        return KickChat(channel, on_message, kick_chatroom_id)
    if platform == "youtube":
        return YouTubeChat(url, on_message)
    return None
