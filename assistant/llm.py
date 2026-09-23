"""Client Ollama : chat avec appel d'outils, sortie JSON contrainte, embeddings.

Tout passe par http://127.0.0.1:11434 : aucune requete ne sort de la machine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterator

import httpx

from .config import OllamaConfig


class OllamaError(RuntimeError):
    pass


@dataclass
class ChatReply:
    content: str
    thinking: str | None
    tool_calls: list[dict[str, Any]]

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLM:
    def __init__(self, cfg: OllamaConfig) -> None:
        self.cfg = cfg
        self._client = httpx.Client(base_url=cfg.host, timeout=cfg.request_timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> LLM:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ sante

    def available_models(self) -> list[str]:
        try:
            resp = self._client.get("/api/tags")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise OllamaError(
                f"Ollama est injoignable sur {self.cfg.host}. Lance-le (`ollama serve`) puis reessaie.\n{exc}"
            ) from exc
        return [m["name"] for m in resp.json().get("models", [])]

    def check_models(self) -> list[str]:
        """Retourne la liste des modeles configures mais absents localement."""
        installed = set(self.available_models())
        wanted = [self.cfg.chat_model, self.cfg.triage_model, self.cfg.embed_model]
        # Ollama affiche "modele:tag" ; un nom sans tag correspond a ":latest".
        normalised = {n if ":" in n else f"{n}:latest" for n in installed}
        return [w for w in wanted if (w if ":" in w else f"{w}:latest") not in normalised]

    # ------------------------------------------------------------------- chat

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        num_ctx: int | None = None,
        temperature: float = 0.3,
        think: bool = False,
        fmt: dict[str, Any] | None = None,
    ) -> ChatReply:
        payload: dict[str, Any] = {
            "model": model or self.cfg.chat_model,
            "messages": messages,
            "stream": False,
            "think": think,
            "keep_alive": self.cfg.keep_alive,
            "options": {
                "num_ctx": num_ctx or self.cfg.num_ctx,
                "temperature": temperature,
            },
        }
        if tools:
            payload["tools"] = tools
        if fmt:
            payload["format"] = fmt

        data = self._post("/api/chat", payload)
        msg = data.get("message") or {}
        return ChatReply(
            content=msg.get("content") or "",
            thinking=msg.get("thinking"),
            tool_calls=msg.get("tool_calls") or [],
        )

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        num_ctx: int | None = None,
        temperature: float = 0.3,
        think: bool = False,
    ) -> Iterator[str]:
        """Diffuse la reponse morceau par morceau, pour l'affichage au fil de l'eau."""
        payload = {
            "model": model or self.cfg.chat_model,
            "messages": messages,
            "stream": True,
            "think": think,
            "keep_alive": self.cfg.keep_alive,
            "options": {"num_ctx": num_ctx or self.cfg.num_ctx, "temperature": temperature},
        }
        with self._client.stream("POST", "/api/chat", json=payload) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                piece = (chunk.get("message") or {}).get("content")
                if piece:
                    yield piece
                if chunk.get("done"):
                    break

    def json_chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
        *,
        model: str | None = None,
        num_ctx: int | None = None,
    ) -> dict[str, Any]:
        """Chat dont la reponse est contrainte a un schema JSON par Ollama."""
        reply = self.chat(
            messages,
            model=model,
            num_ctx=num_ctx,
            temperature=0.0,
            think=False,
            fmt=schema,
        )
        try:
            return json.loads(reply.content)
        except json.JSONDecodeError as exc:
            raise OllamaError(f"Reponse JSON invalide du modele : {reply.content[:300]}") from exc

    # ------------------------------------------------------------- embeddings

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        data = self._post(
            "/api/embed",
            {
                "model": self.cfg.embed_model,
                "input": texts,
                "keep_alive": self.cfg.keep_alive,
            },
        )
        vectors = data.get("embeddings") or []
        if len(vectors) != len(texts):
            raise OllamaError(f"{len(texts)} textes envoyes, {len(vectors)} embeddings recus")
        dim = len(vectors[0])
        if dim != self.cfg.embed_dim:
            raise OllamaError(
                f"Le modele {self.cfg.embed_model} produit des vecteurs de dimension {dim}, "
                f"la config annonce embed_dim: {self.cfg.embed_dim}. Corrige config.yaml."
            )
        return vectors

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    # ----------------------------------------------------------------- interne

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self._client.post(path, json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500]
            raise OllamaError(f"Ollama a repondu {exc.response.status_code} : {detail}") from exc
        except httpx.HTTPError as exc:
            raise OllamaError(
                f"Ollama est injoignable sur {self.cfg.host}. Verifie qu'il tourne.\n{exc}"
            ) from exc
        return resp.json()
