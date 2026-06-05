"""
Multiplayer Game Server — HTTP + WebSocket on a single port
=============================================================
Uses aiohttp to serve the game HTML page and handle WebSocket
connections for real-time game play.

Supported games: tic-tac-toe (ttt), gomoku, Chinese chess (chess)

HTTP routes:
  GET  /  → game HTML page        (HEAD auto-handled by aiohttp)
  GET  /  → WebSocket upgrade     (game protocol)

Start:  python server.py          (listens on $PORT or 8765)
"""

import asyncio
import json
import logging
import os
import pathlib
import random
import signal
import string
from typing import Optional

from aiohttp import WSCloseCode, web

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("game-server")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HTML_PATH = pathlib.Path(__file__).parent / "tictactoe.html"
MAX_PLAYERS_PER_ROOM = 2
CODE_LENGTH = 4

HOST_SIDES = {
    "ttt": "X",
    "gomoku": "black",
    "chess": "red",
}

JOINER_SIDES = {
    "ttt": "O",
    "gomoku": "white",
    "chess": "black",
}


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def generate_room_code(length: int = CODE_LENGTH) -> str:
    return "".join(random.choices(string.ascii_uppercase, k=length))


# ---------------------------------------------------------------------------
# Player
# ---------------------------------------------------------------------------
class Player:
    def __init__(self, player_id: int, ws: web.WebSocketResponse):
        self.player_id = player_id
        self.ws = ws
        self.ready = False

    async def send(self, data: dict) -> None:
        try:
            await self.ws.send_json(data)
        except Exception:
            logger.warning(f"Failed to send to player {self.player_id}")


# ---------------------------------------------------------------------------
# Room
# ---------------------------------------------------------------------------
class Room:
    def __init__(self, code: str, game: str):
        self.code = code
        self.game = game
        self.players: dict[int, Player] = {}
        self._started = False

    @property
    def is_full(self) -> bool:
        return len(self.players) >= MAX_PLAYERS_PER_ROOM

    @property
    def is_started(self) -> bool:
        return self._started

    @property
    def all_ready(self) -> bool:
        return (
            len(self.players) == MAX_PLAYERS_PER_ROOM
            and all(p.ready for p in self.players.values())
        )

    @property
    def is_empty(self) -> bool:
        return len(self.players) == 0

    def add_player(self, player_id: int, ws: web.WebSocketResponse) -> Player:
        if self.is_full:
            raise ValueError(f"Room {self.code} is already full")
        p = Player(player_id, ws)
        self.players[player_id] = p
        logger.info(f"Room {self.code}: player {player_id} joined ({self.game})")
        return p

    def remove_player(self, player_id: int) -> None:
        self.players.pop(player_id, None)
        logger.info(f"Room {self.code}: player {player_id} left ({len(self.players)} remaining)")

    async def try_start(self) -> None:
        if not self.all_ready or self._started:
            return
        self._started = True
        logger.info(f"Room {self.code}: game starting ({self.game})")
        for pid in (0, 1):
            p = self.players[pid]
            await p.send({
                "type": "game_start",
                "your_turn": pid == 0,
                "your_side": HOST_SIDES[self.game] if pid == 0 else JOINER_SIDES[self.game],
            })

    async def broadcast(self, message: dict, exclude: Optional[int] = None) -> None:
        for pid, p in self.players.items():
            if pid != exclude:
                await p.send(message)

    async def relay_move(self, from_id: int, game: str, position: dict) -> None:
        opponent = self.players.get(1 - from_id)
        if opponent:
            await opponent.send({
                "type": "opponent_move",
                "game": game,
                "position": position,
            })

    async def relay_restart(self, from_id: int) -> None:
        opponent = self.players.get(1 - from_id)
        if opponent:
            await opponent.send({"type": "opponent_restart"})


# ---------------------------------------------------------------------------
# Game Server
# ---------------------------------------------------------------------------
class GameServer:
    def __init__(self):
        self.rooms: dict[str, Room] = {}
        self._player_rooms: dict[int, str] = {}

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        """Main WebSocket handler — one per connected player."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        addr = request.remote
        logger.info(f"[connect] {addr}")

        try:
            async for msg in ws:
                if msg.type != web.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    await ws.send_json({"type": "error", "message": "Invalid JSON"})
                    continue
                await self._dispatch(ws, data)
        except Exception:
            pass  # client disconnected
        finally:
            await self._handle_disconnect(ws)
            logger.info(f"[disconnect] {addr}")
        return ws

    async def _dispatch(self, ws: web.WebSocketResponse, data: dict) -> None:
        msg_type = data.get("type")
        if msg_type == "create_room":
            await self._create(ws, data)
        elif msg_type == "join_room":
            await self._join(ws, data)
        elif msg_type == "ready":
            await self._ready(ws)
        elif msg_type == "move":
            await self._move(ws, data)
        elif msg_type == "restart":
            await self._restart(ws)
        else:
            await ws.send_json({"type": "error", "message": f"Unknown: {msg_type}"})

    # ---- helpers -----------------------------------------------------------
    def _room_for(self, ws: web.WebSocketResponse) -> Optional[Room]:
        code = self._player_rooms.get(id(ws))
        return self.rooms.get(code) if code else None

    def _player_in_room(self, room: Room, ws: web.WebSocketResponse) -> Optional[Player]:
        return next((p for p in room.players.values() if p.ws is ws), None)

    def _create_unique_code(self) -> str:
        while True:
            code = generate_room_code()
            if code not in self.rooms:
                return code

    # ---- message handlers --------------------------------------------------
    async def _create(self, ws: web.WebSocketResponse, data: dict) -> None:
        game = data.get("game")
        if game not in HOST_SIDES:
            await ws.send_json({"type": "error", "message": f"Unsupported: {game}"})
            return
        if self._room_for(ws):
            await ws.send_json({"type": "error", "message": "Already in a room"})
            return

        code = self._create_unique_code()
        room = Room(code, game)
        room.add_player(0, ws)
        self.rooms[code] = room
        self._player_rooms[id(ws)] = code
        logger.info(f"[create] room {code} ({game})")
        await ws.send_json({"type": "room_created", "code": code, "game": game})

    async def _join(self, ws: web.WebSocketResponse, data: dict) -> None:
        code = data.get("code", "").upper()
        room = self.rooms.get(code)
        if room is None:
            await ws.send_json({"type": "error", "message": f"Room '{code}' not found"})
            return
        if self._room_for(ws):
            await ws.send_json({"type": "error", "message": "Already in a room"})
            return
        if room.is_full:
            await ws.send_json({"type": "error", "message": f"Room '{code}' is full"})
            return

        room.add_player(1, ws)
        self._player_rooms[id(ws)] = code
        logger.info(f"[join] room {code}")
        await ws.send_json({"type": "joined", "game": room.game})

    async def _ready(self, ws: web.WebSocketResponse) -> None:
        room = self._room_for(ws)
        if room is None:
            await ws.send_json({"type": "error", "message": "Not in a room"})
            return
        player = self._player_in_room(room, ws)
        if player is None:
            return
        player.ready = True
        logger.info(f"Room {room.code}: player {player.player_id} ready")
        await room.broadcast({"type": "player_ready", "player": player.player_id})
        await room.try_start()

    async def _move(self, ws: web.WebSocketResponse, data: dict) -> None:
        room = self._room_for(ws)
        if room is None:
            await ws.send_json({"type": "error", "message": "Not in a room"})
            return
        player = self._player_in_room(room, ws)
        if player is None:
            return
        game = data.get("game", room.game)
        position = data.get("position", {})
        await room.relay_move(player.player_id, game, position)

    async def _restart(self, ws: web.WebSocketResponse) -> None:
        room = self._room_for(ws)
        if room is None:
            await ws.send_json({"type": "error", "message": "Not in a room"})
            return
        room._started = False
        for p in room.players.values():
            p.ready = False
        player = self._player_in_room(room, ws)
        if player:
            await room.relay_restart(player.player_id)

    async def _handle_disconnect(self, ws: web.WebSocketResponse) -> None:
        room = self._room_for(ws)
        if room is None:
            return
        player = self._player_in_room(room, ws)
        if player is None:
            return
        room.remove_player(player.player_id)
        self._player_rooms.pop(id(ws), None)
        for p in room.players.values():
            await p.send({"type": "opponent_disconnected"})
        if room.is_empty:
            del self.rooms[room.code]
            logger.info(f"Room {room.code}: deleted (empty)")


# ---------------------------------------------------------------------------
# HTTP handler — serve the game HTML page
# ---------------------------------------------------------------------------
async def serve_html(_request: web.Request) -> web.Response:
    """Serve the game HTML page at GET /."""
    html = HTML_PATH.read_text(encoding="utf-8")
    return web.Response(text=html, content_type="text/html; charset=utf-8")


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------
def create_app() -> web.Application:
    """Create and configure the aiohttp application."""
    app = web.Application()
    game = GameServer()

    # Single GET / route that dispatches:
    #   Upgrade: websocket → game WebSocket handler
    #   Otherwise          → HTML page
    # aiohttp auto-handles HEAD for health checks.
    async def root_handler(request: web.Request) -> web.StreamResponse:
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await game.handle_ws(request)
        return await serve_html(request)

    app.router.add_get("/", root_handler)
    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    port = int(os.environ.get("PORT", "8765"))
    logger.info(f"Starting game server on 0.0.0.0:{port}")
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=port, print=lambda _: None)


if __name__ == "__main__":
    main()
