"""
WebSocket Game Server for Online Multiplayer
============================================
Uses asyncio + websockets to manage rooms, relay moves, and handle
game lifecycle (create → join → ready → play → disconnect).

Supported games: tic-tac-toe (ttt), gomoku, Chinese chess (chess)
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

import websockets
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import InvalidMessage

# ---------------------------------------------------------------------------
# Custom ServerConnection — handles HEAD requests (Render health checks)
# ---------------------------------------------------------------------------
class GameServerConnection(ServerConnection):
    """Extended ServerConnection: tolerates non-WebSocket HTTP requests.

    Render's load balancer sends HEAD requests for health checks, and
    the websockets parser rejects any method other than GET.  We catch
    every handshake-level InvalidMessage, send a minimal 200 OK, and
    let the connection close gracefully — this keeps Render happy while
    real browsers get the HTML page via `process_request`.
    """

    async def handshake(self, process_request=None, process_response=None, server_header=None):
        try:
            return await super().handshake(
                process_request, process_response, server_header
            )
        except InvalidMessage:
            # Any non-WebSocket HTTP request (HEAD, POST, bad GET, …).
            # Write a 200 OK and close the transport so the response
            # is flushed before conn_handler cleans up the connection.
            try:
                transport = self.transport
                transport.write(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
                )
                transport.close()  # flush & close
            except Exception:
                pass
            raise


# ---------------------------------------------------------------------------
# Static file serving (HTTP GET / → game HTML page)
# ---------------------------------------------------------------------------
HTML_PATH = pathlib.Path(__file__).parent / "tictactoe.html"

from websockets.http11 import Response as HTTPResponse  # noqa: E402


async def process_request(connection, request):
    """Handle HTTP GET / — return the game HTML; all else → WebSocket."""
    if request.path == "/":
        try:
            html = HTML_PATH.read_text(encoding="utf-8")
            headers = [("Content-Type", "text/html; charset=utf-8")]
            return HTTPResponse(200, "OK", headers, html.encode("utf-8"))
        except Exception:
            logger.warning(f"Failed to serve {HTML_PATH}")
    return None  # fall through → WebSocket handshake


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
MAX_PLAYERS_PER_ROOM = 2
CODE_LENGTH = 4

# Which side the host (player 0) gets, per game type.
# The joiner (player 1) automatically receives the opposite side.
HOST_SIDES = {
    "ttt": "X",       # X always goes first in TTT
    "gomoku": "black",  # Black always goes first in Gomoku
    "chess": "red",    # Red always moves first in Chinese chess
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
    """Generate a random uppercase alphanumeric room code."""
    return "".join(random.choices(string.ascii_uppercase, k=length))


# ---------------------------------------------------------------------------
# Player
# ---------------------------------------------------------------------------
class Player:
    """Represents one connected player inside a room."""

    def __init__(self, player_id: int, websocket: ServerConnection):
        self.player_id = player_id       # 0 = host, 1 = joiner
        self.websocket = websocket
        self.ready = False

    async def send(self, data: dict) -> None:
        """Send a JSON message to this player."""
        try:
            await self.websocket.send(json.dumps(data))
        except websockets.exceptions.ConnectionClosed:
            logger.warning(f"Failed to send to player {self.player_id}: connection closed")


# ---------------------------------------------------------------------------
# Room
# ---------------------------------------------------------------------------
class Room:
    """A game room holding up to 2 players.

    Lifecycle:
      created → joined → (both ready) → playing → (both gone) → deleted
    """

    def __init__(self, code: str, game: str):
        self.code = code
        self.game = game
        self.players: dict[int, Player] = {}  # 0=host, 1=joiner
        self._started = False

    # ---- properties -------------------------------------------------------
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

    @property
    def host(self) -> Optional[Player]:
        return self.players.get(0)

    @property
    def joiner(self) -> Optional[Player]:
        return self.players.get(1)

    # ---- player management ------------------------------------------------
    def add_player(self, player_id: int, websocket: ServerConnection) -> Player:
        """Create and register a new player. Raises ValueError if full."""
        if self.is_full:
            raise ValueError(f"Room {self.code} is already full")
        player = Player(player_id, websocket)
        self.players[player_id] = player
        logger.info(f"Room {self.code}: player {player_id} joined (game={self.game})")
        return player

    def remove_player(self, player_id: int) -> None:
        """Remove a player (e.g. on disconnect)."""
        self.players.pop(player_id, None)
        logger.info(f"Room {self.code}: player {player_id} left ({len(self.players)} remaining)")

    # ---- game flow --------------------------------------------------------
    async def try_start(self) -> None:
        """If both players are ready, broadcast game_start and begin."""
        if not self.all_ready or self._started:
            return

        self._started = True
        logger.info(f"Room {self.code}: game starting (game={self.game})")

        # Host (player 0) always gets the first turn
        await self.host.send({
            "type": "game_start",
            "your_turn": True,
            "your_side": HOST_SIDES[self.game],
        })
        await self.joiner.send({
            "type": "game_start",
            "your_turn": False,
            "your_side": JOINER_SIDES[self.game],
        })

    async def broadcast(self, message: dict, exclude_id: Optional[int] = None) -> None:
        """Send a message to every player in the room except exclude_id."""
        for pid, player in self.players.items():
            if pid != exclude_id:
                await player.send(message)

    async def relay_move(self, from_id: int, game: str, position: dict) -> None:
        """Relay a move from one player to the opponent."""
        opponent_id = 1 - from_id  # 0→1, 1→0
        opponent = self.players.get(opponent_id)
        if opponent:
            await opponent.send({
                "type": "opponent_move",
                "game": game,
                "position": position,
            })

    async def relay_restart(self, from_id: int) -> None:
        """Relay a restart request to the opponent."""
        opponent_id = 1 - from_id
        opponent = self.players.get(opponent_id)
        if opponent:
            await opponent.send({"type": "opponent_restart"})


# ---------------------------------------------------------------------------
# Game Server
# ---------------------------------------------------------------------------
class GameServer:
    """Top-level server: manages rooms and routes incoming messages."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8765):
        self.host = host
        self.port = port
        self.rooms: dict[str, Room] = {}          # code → Room
        self._player_rooms: dict[int, str] = {}   # id(player) → room_code

    # ---- room management --------------------------------------------------
    def _room_for_player(self, websocket: ServerConnection) -> Optional[Room]:
        """Look up the room a player currently belongs to."""
        code = self._player_rooms.get(id(websocket))
        if code:
            return self.rooms.get(code)
        return None

    def _find_room(self, code: str) -> Optional[Room]:
        """Find a room by code (case-insensitive)."""
        return self.rooms.get(code.upper())

    def _create_unique_code(self) -> str:
        """Generate a code that does not collide with any existing room."""
        while True:
            code = generate_room_code()
            if code not in self.rooms:
                return code

    # ---- connection handler -----------------------------------------------
    async def handle_connection(self, websocket: ServerConnection) -> None:
        """Main entry point for every new WebSocket connection."""
        addr = websocket.remote_address
        logger.info(f"[connect] {addr}")

        try:
            async for raw in websocket:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    await self._send(websocket, {
                        "type": "error",
                        "message": "Invalid JSON",
                    })
                    continue

                await self._dispatch(websocket, data)

        except websockets.exceptions.ConnectionClosed:
            pass  # normal disconnect
        finally:
            await self._handle_disconnect(websocket)
            logger.info(f"[disconnect] {addr}")

    # ---- message dispatcher -----------------------------------------------
    async def _dispatch(self, websocket: ServerConnection, data: dict) -> None:
        """Route an incoming JSON message to the appropriate handler."""
        msg_type = data.get("type")

        if msg_type == "create_room":
            await self._handle_create(websocket, data)

        elif msg_type == "join_room":
            await self._handle_join(websocket, data)

        elif msg_type == "ready":
            await self._handle_ready(websocket)

        elif msg_type == "move":
            await self._handle_move(websocket, data)

        elif msg_type == "restart":
            await self._handle_restart(websocket)

        else:
            await self._send(websocket, {
                "type": "error",
                "message": f"Unknown message type: {msg_type}",
            })

    # ---- handlers ---------------------------------------------------------
    async def _handle_create(self, websocket: ServerConnection, data: dict) -> None:
        """Handle create_room: allocate a new room and register the player as host."""
        game = data.get("game")
        if game not in HOST_SIDES:
            await self._send(websocket, {
                "type": "error",
                "message": f"Unsupported game: {game}. Supported: ttt, gomoku, chess",
            })
            return

        # Prevent a player from being in two rooms at once
        if self._room_for_player(websocket):
            await self._send(websocket, {
                "type": "error",
                "message": "You are already in a room",
            })
            return

        code = self._create_unique_code()
        room = Room(code, game)
        room.add_player(0, websocket)
        self.rooms[code] = room
        self._player_rooms[id(websocket)] = code

        logger.info(f"[create] room {code} created by {websocket.remote_address} (game={game})")
        await self._send(websocket, {
            "type": "room_created",
            "code": code,
            "game": game,
        })

    async def _handle_join(self, websocket: ServerConnection, data: dict) -> None:
        """Handle join_room: add the player to an existing room."""
        code = data.get("code", "").upper()
        room = self._find_room(code)

        if room is None:
            await self._send(websocket, {
                "type": "error",
                "message": f"Room '{code}' not found",
            })
            return

        if self._room_for_player(websocket):
            await self._send(websocket, {
                "type": "error",
                "message": "You are already in a room",
            })
            return

        if room.is_full:
            await self._send(websocket, {
                "type": "error",
                "message": f"Room '{code}' is full",
            })
            return

        room.add_player(1, websocket)
        self._player_rooms[id(websocket)] = code

        logger.info(f"[join] {websocket.remote_address} joined room {code}")
        await self._send(websocket, {
            "type": "joined",
            "game": room.game,
        })

    async def _handle_ready(self, websocket: ServerConnection) -> None:
        """Handle ready: mark the player as ready and try to start the game."""
        room = self._room_for_player(websocket)
        if room is None:
            await self._send(websocket, {
                "type": "error",
                "message": "You are not in a room",
            })
            return

        player = next(
            (p for p in room.players.values() if p.websocket is websocket), None
        )
        if player is None:
            return

        player.ready = True
        logger.info(f"Room {room.code}: player {player.player_id} is ready")

        # Notify both players of readiness
        await room.broadcast({
            "type": "player_ready",
            "player": player.player_id,
        })

        await room.try_start()

    async def _handle_move(self, websocket: ServerConnection, data: dict) -> None:
        """Handle move: relay the move to the opponent."""
        room = self._room_for_player(websocket)
        if room is None:
            await self._send(websocket, {
                "type": "error",
                "message": "You are not in a room",
            })
            return

        player = next(
            (p for p in room.players.values() if p.websocket is websocket), None
        )
        if player is None:
            return

        game = data.get("game", room.game)
        position = data.get("position", {})
        await room.relay_move(player.player_id, game, position)

    async def _handle_restart(self, websocket: ServerConnection) -> None:
        """Handle restart: relay the restart request to the opponent."""
        room = self._room_for_player(websocket)
        if room is None:
            await self._send(websocket, {
                "type": "error",
                "message": "You are not in a room",
            })
            return

        # Reset room state so players can ready again
        room._started = False
        for p in room.players.values():
            p.ready = False

        player = next(
            (p for p in room.players.values() if p.websocket is websocket), None
        )
        if player:
            await room.relay_restart(player.player_id)

    async def _handle_disconnect(self, websocket: ServerConnection) -> None:
        """Clean up when a player disconnects."""
        room = self._room_for_player(websocket)
        if room is None:
            return

        # Find which player disconnected
        player = next(
            (p for p in room.players.values() if p.websocket is websocket), None
        )
        if player is None:
            return

        room.remove_player(player.player_id)
        self._player_rooms.pop(id(websocket), None)

        # Notify the remaining player
        remaining = [p for p in room.players.values()]
        if remaining:
            await remaining[0].send({"type": "opponent_disconnected"})

        # Auto-delete room when empty
        if room.is_empty:
            del self.rooms[room.code]
            logger.info(f"Room {room.code}: deleted (empty)")

    # ---- helpers ----------------------------------------------------------
    @staticmethod
    async def _send(websocket: ServerConnection, data: dict) -> None:
        """Send a JSON message to a single websocket."""
        try:
            await websocket.send(json.dumps(data))
        except websockets.exceptions.ConnectionClosed:
            pass

    # ---- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        """Start the server and serve forever."""
        logger.info(f"Starting game server on {self.host}:{self.port}")
        stop_future = asyncio.get_running_loop().create_future()

        shutdown = lambda: self._set_stop(stop_future)

        async with websockets.serve(
            self.handle_connection, self.host, self.port,
            process_request=process_request,
            create_connection=GameServerConnection,
        ):
            logger.info("Server is ready — awaiting connections...")
            await stop_future

    def _set_stop(self, future: asyncio.Future) -> None:
        if not future.done():
            future.set_result(None)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def main() -> None:
    port = int(os.environ.get("PORT", "8765"))
    server = GameServer(host="0.0.0.0", port=port)

    # Attach graceful shutdown for SIGINT / SIGTERM
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: None)
        except NotImplementedError:
            # Windows does not support add_signal_handler for SIGTERM
            pass

    # On Windows, use a simpler approach: catch KeyboardInterrupt
    try:
        await server.start()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        logger.info("Server stopped.")


if __name__ == "__main__":
    asyncio.run(main())
